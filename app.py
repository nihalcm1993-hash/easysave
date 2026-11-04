import html
import re
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
CHUNK = 64 * 1024
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


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


def clean_error(e):
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).strip()
    msg = re.sub(r'^ERROR:\s*', '', msg)
    low = msg.lower()
    if 'sign in to confirm' in low or 'login required' in low or 'rate-limit' in low:
        msg += ' (The site asked for a login or blocked the request. Try again later.)'
    elif 'unable to extract' in low or 'unsupported url' in low:
        msg += ' (Try updating the downloader: pip install -U yt-dlp)'
    return msg[:400]


def err(message, status=500):
    return jsonify({'error': message}), status


# ---------------------------------------------------------------- spotify

def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(800_000).decode('utf-8', 'replace')


def parse_meta(page):
    meta = {}
    for tag in re.findall(r'<meta\s[^>]*>', page, flags=re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag))
        key = attrs.get('property') or attrs.get('name')
        if key and 'content' in attrs and key not in meta:
            meta[key] = html.unescape(attrs['content'])
    return meta


def spotify_track_info(url):
    """Read the public Spotify page of a track: title, artist, duration (s)."""
    final_url, page = http_get(url)
    fp = urlparse(final_url)
    if (fp.hostname or '').lower() != 'open.spotify.com':
        raise ValueError('Please paste the full open.spotify.com track link.')
    if '/track/' not in fp.path:
        raise ValueError('Only single Spotify track links are supported '
                         '(not playlists, albums or artists).')

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


def find_youtube_match(artist, title, duration):
    query = f'ytsearch5:{artist} {title} audio'.strip()
    with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True, 'skip_download': True}) as ydl:
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
    cmd = ['ffmpeg', '-loglevel', 'error', '-nostdin']
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


@app.route('/download', methods=['GET'])
def download_video():
    url = request.args.get('url', '').strip()
    download_type = request.args.get('type', 'video')
    token = request.args.get('token', '')

    try:
        quality = int(request.args.get('quality', DEFAULT_QUALITY))
    except ValueError:
        quality = DEFAULT_QUALITY
    if quality not in QUALITIES:
        quality = DEFAULT_QUALITY

    if not url:
        return err('URL is required', 400)
    if not is_allowed(url):
        return err('Only YouTube, Instagram or Spotify track links are supported!', 400)

    spotify = is_spotify(url)
    is_audio = download_type == 'audio' or spotify   # Spotify is always MP3

    tags = {}
    source_url = url
    if spotify:
        try:
            sp = spotify_track_info(url)
            source_url = find_youtube_match(sp['artist'], sp['title'], sp['duration'])
        except ValueError as e:
            return err(str(e), 400)
        except Exception as e:
            return err("Couldn't read that Spotify link: " + clean_error(e), 502)
        tags = {'title': sp['title'], 'artist': sp['artist']}

    # Only fetch metadata + direct stream links (fast, nothing saved to disk)
    ydl_opts = {
        'format': 'bestaudio/best' if is_audio
        else 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b',
        'noplaylist': True,
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception as e:
        return err(clean_error(e))

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
