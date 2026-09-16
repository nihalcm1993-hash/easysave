import os
import re
from flask import Flask, request, send_file, render_template_string
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "EasySave")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def clean_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title)

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EasySave Downloader</title>
    <style>
        * { box-sizing: border-box; font-family: 'Segoe UI', system-ui, sans-serif; }
        body {
            background: #0f172a;
            color: #1e293b;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background-image: linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5)), url('https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?q=80&w=1920&auto=format&fit=crop');
            background-size: cover;
            background-position: center;
        }
        .card {
            background: #ffffff;
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            width: 480px;
            text-align: center;
        }
        h1 { margin: 0 0 15px 0; font-size: 22px; color: #1e293b; display: flex; align-items: center; justify-content: center; gap: 8px; }
        input {
            width: 100%;
            padding: 14px 16px;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 10px;
            color: #1e293b;
            font-size: 14px;
            outline: none;
            margin-bottom: 20px;
        }
        input:focus { border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); }
        button {
            width: 100%;
            padding: 14px;
            border-radius: 10px;
            border: none;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            color: white;
            background: #3b82f6;
            margin-bottom: 12px;
            transition: background 0.2s, transform 0.1s;
        }
        button:hover { background: #2563eb; }
        button:active { transform: scale(0.98); }
        .btn-audio { background: #10b981; }
        .btn-audio:hover { background: #059669; }
        #status { margin-top: 15px; font-size: 13px; color: #64748b; font-weight: 500; }
    </style>
</head>
<body>
    <div class="card">
        <h1>🎬 EasySave Downloader</h1>
        
        <form id="downloadForm" method="POST" action="/download" onsubmit="showLoading()">
            <input type="text" id="url" name="url" placeholder="https://www.youtube.com/watch?v=..." required>
            <button type="submit" onclick="document.getElementById('downloadForm').action='/download'">📥 Download HD Video</button>
            <button type="submit" class="btn-audio" onclick="document.getElementById('downloadForm').action='/audio'">🎵 Download MP3 Audio</button>
        </form>
        
        <div id="status">Saves directly to Downloads/EasySave</div>
    </div>

    <script>
        function showLoading() {
            document.getElementById('status').innerText = '⏳ Downloading... Please wait a moment.';
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

@app.route('/download', methods=['POST'])
def download_video():
    url = request.form.get('url')
    if not url:
        return "No URL provided", 400

    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return send_file(filename, as_attachment=True)

    except Exception as e:
        return f"Download Error: {str(e)}", 500

@app.route('/audio', methods=['POST'])
def download_audio():
    url = request.form.get('url')
    if not url:
        return "No URL provided", 400

    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base_name = ydl.prepare_filename(info)
            mp3_name = os.path.splitext(base_name)[0] + '.mp3'
            return send_file(mp3_name, as_attachment=True)

    except Exception as e:
        return f"Audio Error: {str(e)}", 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)