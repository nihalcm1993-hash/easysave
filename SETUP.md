# EasySave setup notes

Folder layout:

```
easysave/
  app.py
  templates/index.html
  cookies.txt        (optional, see step 3)
```

## 1. Install / update (do this first)

```
pip install -U flask "yt-dlp[default]"
```

- `ffmpeg` must be installed and in your PATH.
- Install **Node.js** (https://nodejs.org) or **Deno**. YouTube downloads need one of them now.
- Restart the app after updating: `python app.py`

## 2. Open it

- On your PC: http://localhost:5000
- On your phone (same Wi-Fi): http://YOUR-PC-IP:5000

## 3. If YouTube says "Sign in to confirm you're not a bot"

Do 1 first. If it still happens, give the app your own YouTube login cookies.
Use a spare Google account if you can, because heavy downloading on a logged-in
account can get that account flagged.

**Easiest: read cookies straight from a browser (Firefox works best)**

1. Log in to YouTube in Firefox.
2. Start the app with the browser name:
   - Windows (PowerShell): `$env:EASYSAVE_BROWSER="firefox"; python app.py`
   - Mac/Linux: `EASYSAVE_BROWSER=firefox python app.py`

Chrome and Edge on Windows often block this (their cookies are locked while the
browser runs). If that fails, use the file method.

**File method**

1. Install a "cookies.txt" export extension in your browser.
2. Open youtube.com while logged in, export cookies in Netscape format.
3. Save the file as `cookies.txt` next to `app.py`. Restart the app.

Cookies expire. If the error comes back after some weeks, export a fresh file.
Never share `cookies.txt` with anyone: it is a key to your account.

## 4. Spotify

- Single songs, playlists and albums work. Spotify files themselves are
  copy-protected, so EasySave finds each song on YouTube and saves an MP3
  with the title and artist filled in.
- Playlists open a list. Tap the arrow on one song, or Download All.
  Your browser may ask to allow multiple downloads: tap Allow.
- Playlist reading uses Spotify's public pages, so it works only for public
  playlists and may break if Spotify changes them.
