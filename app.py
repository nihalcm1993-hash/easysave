import html
import json
import os
import re
import shutil
import subprocess
import urllib.request
from urllib.parse import urlparse, quote

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import yt_dlp

app = Flask(__name__, template_folder='templates')

YOUTUBE_HOSTS = ('youtube.com', 'youtu.be')
INSTAGRAM_HOSTS = ('instagram.com', 'instagr.am')
SPOTIFY_HOSTS = ('open.spotify.com', 'spotify.link')
ALLOWED_HOSTS = YOUTUBE_HOSTS + INSTAGRAM_HOSTS + SPOTIFY_HOSTS

QUALITIES = (128, 192, 256, 320)
DEFAULT_QUALITY = 320
MAX_VIDEO_HEIGHT = 1080          # 4K files are huge and slow; 1080p is the sweet spot
CHUNK = 256 * 1024
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

# ---------------------------------------------------------------- YouTube login ("not a bot") fix
# Option 1: put a Netscape-format cookies.txt next to this file (or set EASYSAVE_COOKIES=/path/cookies.txt)
# Option 2: set EASYSAVE_BROWSER=firefox (or chrome, edge, brave, safari...) to read cookies from that browser
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.environ.get('EASYSAVE_COOKIES') or os.path.join(HERE, 'cookies.txt')
COOKIES_BROWSER = os.environ.get('EASYSAVE_BROWSER', '').strip().lower()

BOT_HELP = ("YouTube is asking this server to prove it's not a bot. Fix on the PC that runs EasySave: "
            "(1) update the downloader: pip install -U \"yt-dlp[default]\" and make sure Node.js or Deno "
            "is installed, then restart. (2) If it still happens, save your YouTube cookies as cookies.txt "
            "next to app.py, or start the app with EASYSAVE_BROWSER=firefox. See the setup notes.")


def base_opts():
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'remote_components': ['ejs:github']}
    runtimes = {}
    for name in ('deno', 'node'):
        if shutil.which(name):
            runtimes[name] = {}
    if runtimes:
        opts['js_runtimes'] = runtimes
    if os.path.isfile(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    elif COOKIES_BROWSER:
        opts['cookiesfrombrowser'] = (COOKIES_BROWSER,)
    return opts


# ---------------------------------------------------------------- helpers

def host_matches(host, hosts):
    return any(host == h or host.endswith('.' + h) for h in hosts)


def get_host(url):
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return None
        return (p.hostname or '').lower()
    except Exception:
        return None


def is_allowed(url):
    host = get_host(url)
    return bool(host) and host_matches(host, ALLOWED_HOSTS)


def is_spotify(url):
    host = get_host(url)
    return bool(host) and host_matches(host, SPOTIFY_HOSTS)


def safe_name(title):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', '', title or '').strip()[:80]
    return name or 'download'


def header_string(fmt):
    headers = fmt.get('http_headers') or {}
    return ''.join(f'{k}: {v}\r\n' for k, v in headers.items())


def is_bot_check(msg):
    low = msg.lower()
    return ('confirm you' in low and 'bot' in low) or 'use --cookies' in low


def clean_error(e):
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).strip()
    msg = re.sub(r'^ERROR:\s*', '', msg)
    low = msg.lower()
    if is_bot_check(msg):
        return BOT_HELP
    if 'login required' in low or 'rate-limit' in low:
        msg += ' (The site asked for a login or blocked the request. Try again later.)'
    elif 'unable to extract' in low or 'unsupported url' in low:
        msg += ' (Try updating the downloader: pip install -U "yt-dlp[default]")'
    return msg[:500]


def err(message, status=500, code=None):
    body = {'error': message}
    if code:
        body['code'] = code
    return jsonify(body), status


# ---------------------------------------------------------------- spotify

def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(2_500_000).decode('utf-8', 'replace')


def parse_meta(page):
    meta = {}
    for tag in re.findall(r'<meta\s[^>]*>', page, flags=re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag))
        key = attrs.get('property') or attrs.get('name')
        if key and 'content' in attrs and key not in meta:
            meta[key] = html.unescape(attrs['content'])
    return meta


def spotify_kind_and_id(final_url):
    p = urlparse(final_url)
    if (p.hostname or '').lower() != 'open.spotify.com':
        raise ValueError('Please paste the full open.spotify.com link.')
    m = re.search(r'/(track|playlist|album)/([A-Za-z0-9]{10,40})', p.path)
    if not m:
        raise ValueError('That Spotify link is not a track, playlist or album.')
    return m.group(1), m.group(2)


def spotify_track_info(page):
    """Read the public Spotify track page: title, artist, duration (s)."""
    meta = parse_meta(page)
    title = meta.get('og:title', '').strip()
    artist = ''

    page_title = re.search(r'<title>(.*?)</title>', page, flags=re.S | re.I)
    if page_title:
        t = html.unescape(page_title.group(1))
        m = re.search(r'by\s+(.+?)\s*\|\s*Spotify', t)
        if m:
            artist = m.group(1).strip()
        if not title:
            title = re.split(r'\s+-\s+(?:song|single)', t)[0].strip()

    if not artist:
        desc = re.sub(r'^Listen to .*? on Spotify\.\s*', '', meta.get('og:description', ''))
        parts = [p.strip() for p in desc.split('·') if p.strip()]
        if parts:
            artist = parts[1] if (parts[0].lower() == title.lower() and len(parts) > 1) else parts[0]

    if not title:
        raise ValueError("Couldn't read the song name from that Spotify link.")

    duration = None
    try:
        duration = int(float(meta.get('music:duration', '')))
    except ValueError:
        pass
    return {'title': title, 'artist': artist, 'duration': duration}


def find_key(obj, key):
    """Depth-first search for the first dict that has `key`."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def spotify_collection(kind, sid):
    """Track list of a public playlist/album, read from Spotify's public embed page."""
    _, page = http_get(f'https://open.spotify.com/embed/{kind}/{sid}')
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, flags=re.S)
    if not m:
        raise ValueError("Couldn't read that Spotify " + kind + ". It may be private or Spotify changed its page.")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise ValueError("Couldn't read that Spotify " + kind + ".")

    holder = find_key(data, 'trackList')
    if not holder or not isinstance(holder.get('trackList'), list):
        raise ValueError("Couldn't find the track list. The " + kind + " may be private or empty.")

    name = str(holder.get('name') or holder.get('title') or kind.title())
    tracks = []
    for t in holder['trackList']:
        if not isinstance(t, dict) or not t.get('title'):
            continue
        dur = t.get('duration')
        tracks.append({
            'title': str(t['title'])[:200],
            'artist': str(t.get('subtitle') or t.get('artist') or '')[:200],
            'duration': int(dur // 1000) if isinstance(dur, (int, float)) and dur > 1000 else None,
        })
    if not tracks:
        raise ValueError('That ' + kind + ' has no playable tracks I could read.')
    return {'type': kind, 'name': name[:120], 'tracks': tracks[:200]}


def find_youtube_match(artist, title, duration):
    query = f'ytsearch5:{artist} {title} audio'.strip()
    opts = base_opts()
    opts.update({'extract_flat': True, 'skip_download': True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(query, download=False)
    entries = [e for e in (res.get('entries') or []) if e and e.get('id')]
    if not entries:
        raise ValueError("Couldn't find that song on YouTube.")
    chosen = entries[0]
    if duration:
        for e in entries:
            d = e.get('duration')
            if d and abs(d - duration) <= 6:
                chosen = e
                break
    return f"https://www.youtube.com/watch?v={chosen['id']}"


# ---------------------------------------------------------------- ffmpeg

def build_ffmpeg_cmd(fmts, is_audio, quality, tags):
    cmd = ['ffmpeg', '-loglevel', 'error', '-nostdin', '-probesize', '8M', '-analyzeduration', '3M']
    for f in fmts:
        cmd += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']
        hdr = header_string(f)
        if hdr:
            cmd += ['-headers', hdr]
        cmd += ['-i', f['url']]

    if is_audio:
        cmd += ['-vn', '-map', '0:a:0',
                '-c:a', 'libmp3lame', '-b:a', f'{quality}k', '-ar', '44100',
                '-id3v2_version', '3', '-write_id3v1', '1', '-write_xing', '0']
        for key in ('title', 'artist'):
            if tags.get(key):
                cmd += ['-metadata', f'{key}={tags[key]}']
        cmd += ['-f', 'mp3', 'pipe:1']
    else:
        if len(fmts) == 2:
            cmd += ['-map', '0:v:0', '-map', '1:a:0']
        cmd += ['-c', 'copy',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-f', 'mp4', 'pipe:1']
    return cmd


# ---------------------------------------------------------------- routes

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/spotify/info', methods=['GET'])
def spotify_info():
    url = request.args.get('url', '').strip()
    if not is_spotify(url):
        return err('That is not a Spotify link.', 400)
    try:
        final_url, page = http_get(url)
        kind, sid = spotify_kind_and_id(final_url)
        if kind == 'track':
            t = spotify_track_info(page)
            return jsonify({'type': 'track', 'name': t['title'], 'tracks': [t]})
        return jsonify(spotify_collection(kind, sid))
    except ValueError as e:
        return err(str(e), 400)
    except Exception as e:
        return err("Couldn't read that Spotify link: " + clean_error(e), 502)


@app.route('/download', methods=['GET'])
def download_video():
    url = request.args.get('url', '').strip()
    download_type = request.args.get('type', 'video')
    token = request.args.get('token', '')
    sp_title = request.args.get('sp_title', '').strip()[:200]
    sp_artist = request.args.get('sp_artist', '').strip()[:200]
    try:
        sp_dur = int(request.args.get('sp_dur', '')) or None
    except ValueError:
        sp_dur = None

    try:
        quality = int(request.args.get('quality', DEFAULT_QUALITY))
    except ValueError:
        quality = DEFAULT_QUALITY
    if quality not in QUALITIES:
        quality = DEFAULT_QUALITY

    spotify = bool(sp_title) or (bool(url) and is_spotify(url))
    if not spotify:
        if not url:
            return err('URL is required', 400)
        if not is_allowed(url):
            return err('Only YouTube, Instagram or Spotify links are supported!', 400)
    is_audio = download_type == 'audio' or spotify   # Spotify is always MP3

    tags = {}
    source_url = url
    try:
        if spotify:
            if not sp_title:                      # plain track link pasted without the page lookup
                final_url, page = http_get(url)
                kind, _ = spotify_kind_and_id(final_url)
                if kind != 'track':
                    return err('This is a ' + kind + ' link. Open it in the page so you can pick tracks.', 400)
                t = spotify_track_info(page)
                sp_title, sp_artist, sp_dur = t['title'], t['artist'], t['duration']
            source_url = find_youtube_match(sp_artist, sp_title, sp_dur)
            tags = {'title': sp_title, 'artist': sp_artist}
    except ValueError as e:
        return err(str(e), 400)
    except Exception as e:
        msg = clean_error(e)
        return err(msg if msg == BOT_HELP else "Couldn't find that song: " + msg, 502,
                   'bot_check' if msg == BOT_HELP else None)

    # Only fetch metadata + direct stream links (fast, nothing saved to disk)
    ydl_opts = base_opts()
    h = MAX_VIDEO_HEIGHT
    ydl_opts['format'] = ('bestaudio/best' if is_audio else
                          f'bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/'
                          f'bv*[height<={h}]+ba/b[height<={h}]/b')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception as e:
        msg = clean_error(e)
        return err(msg, 500, 'bot_check' if msg == BOT_HELP else None)

    fmts = info.get('requested_formats') or [info]
    if is_audio:
        fmts = fmts[:1]
        if not tags:
            tags = {'title': info.get('title') or '',
                    'artist': re.sub(r'\s*-\s*Topic$', '', info.get('uploader') or '')}

    if spotify and tags.get('artist'):
        title = safe_name(f"{tags['artist']} - {tags['title']}")
    elif spotify:
        title = safe_name(tags['title'])
    else:
        title = safe_name(info.get('title'))

    cmd = build_ffmpeg_cmd(fmts, is_audio, quality, tags)
    ext, mimetype = ('mp3', 'audio/mpeg') if is_audio else ('mp4', 'video/mp4')

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return err('ffmpeg is not installed or not in PATH')

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            # Stops ffmpeg if the user cancels the download
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()

    filename = f'{title}.{ext}'
    ascii_name = filename.encode('ascii', 'ignore').decode() or f'download.{ext}'
    disposition = (f'attachment; filename="{ascii_name}"; '
                   f"filename*=UTF-8''{quote(filename)}")

    resp = Response(
        stream_with_context(generate()),
        mimetype=mimetype,
        headers={
            'Content-Disposition': disposition,
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        },
    )
    # The page watches for this cookie to know the download has started
    if re.fullmatch(r'[A-Za-z0-9-]{1,64}', token):
        resp.set_cookie('dl_token', token, max_age=120, path='/', samesite='Lax')
    return resp


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
