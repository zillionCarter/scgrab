cat > /home/claude/scgrab/app.py << 'PYEOF'
import os
import uuid
import threading
import time
import zipfile
from flask import Flask, request, jsonify, send_file, render_template
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()

def job_get(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {})) or None

def job_set(job_id, data):
    with jobs_lock:
        jobs[job_id] = data

def job_update(job_id, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)

def job_delete(job_id):
    with jobs_lock:
        jobs.pop(job_id, None)

def get_proxy():
    return os.environ.get('PROXY_URL') or None

def base_ydl_opts():
    opts = {'quiet': True, 'no_warnings': True}
    proxy = get_proxy()
    if proxy:
        opts['proxy'] = proxy
    return opts

def cleanup_job(job_id, delay=600):
    def _cleanup():
        time.sleep(delay)
        import shutil
        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        job = job_get(job_id) or {}
        zip_path = job.get('zip')
        if zip_path and os.path.exists(zip_path):
            try: os.remove(zip_path)
            except: pass
        job_delete(job_id)
    threading.Thread(target=_cleanup, daemon=True).start()

# ── Fetch metadata (fast flat extract) ──────────────────────────────────────
def run_info(job_id, url):
    opts = base_ydl_opts()
    # extract_flat='in_playlist' only fetches titles/URLs, not full track info
    # This is MUCH faster and avoids geo-block on metadata fetching
    opts.update({
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'ignoreerrors': True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            job_update(job_id, status='error',
                error='Could not reach SoundCloud. The server may be geo-blocked — set PROXY_URL in Render env vars.')
            return

        uploader = info.get('uploader') or info.get('channel') or ''
        thumbnail = info.get('thumbnail') or ''
        tracks = []

        if info.get('_type') == 'playlist':
            for entry in (info.get('entries') or []):
                if not entry:
                    continue
                # With extract_flat, webpage_url may not be present — build it from id
                track_url = (entry.get('webpage_url')
                             or entry.get('url')
                             or f"https://soundcloud.com/{entry.get('id', '')}").strip()
                thumb = entry.get('thumbnail') or thumbnail
                tracks.append({
                    'title': entry.get('title') or 'Unknown Track',
                    'url': track_url,
                    'uploader': entry.get('uploader') or uploader,
                    'duration': entry.get('duration'),
                    'thumbnail': thumb,
                })
            playlist_title = info.get('title') or 'SoundCloud Playlist'
            playlist_thumb = thumbnail or (tracks[0]['thumbnail'] if tracks else '')
        else:
            # Single track
            tracks.append({
                'title': info.get('title') or 'Unknown Track',
                'url': info.get('webpage_url') or url,
                'uploader': uploader,
                'duration': info.get('duration'),
                'thumbnail': thumbnail,
            })
            playlist_title = info.get('title') or 'SoundCloud Track'
            playlist_thumb = thumbnail

        job_update(job_id, status='done', playlist_data={
            'title': playlist_title,
            'uploader': uploader,
            'thumbnail': playlist_thumb,
            'count': len(tracks),
            'tracks': tracks,
        })

    except Exception as e:
        job_update(job_id, status='error', error=str(e))

# ── Download tracks ──────────────────────────────────────────────────────────
def run_download(job_id, urls, quality, fmt):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    total = len(urls)
    completed = [0]

    def progress_hook(d):
        if d['status'] == 'downloading':
            tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            dl = d.get('downloaded_bytes', 0)
            track_pct = int(dl / tot * 100) if tot else 0
            overall = int((completed[0] / total * 100) + (track_pct / total))
            job_update(job_id,
                progress=overall,
                current_file=os.path.basename(d.get('filename', '')))
        elif d['status'] == 'finished':
            completed[0] += 1
            job_update(job_id, progress=int(completed[0] / total * 100))

    opts = base_ydl_opts()
    opts.update({
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(job_dir, '%(title)s.%(ext)s'),
        'noplaylist': True,
        'ignoreerrors': True,
        'retries': 3,
        'fragment_retries': 3,
        'progress_hooks': [progress_hook],
    })

    if fmt in ('mp3', 'wav'):
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': fmt,
            'preferredquality': quality,
        }]

    try:
        job_update(job_id, status='downloading')
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download(urls)

        all_files = [
            os.path.join(job_dir, f)
            for f in os.listdir(job_dir)
            if os.path.isfile(os.path.join(job_dir, f)) and not f.endswith('.part')
        ]

        if not all_files:
            job_update(job_id, status='error',
                error='No files downloaded. SoundCloud may be geo-blocking this server. Set PROXY_URL in Render.')
            return

        zip_path = None
        if len(all_files) > 1:
            zip_path = os.path.join(DOWNLOAD_DIR, f'{job_id}.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in all_files:
                    zf.write(f, os.path.basename(f))

        job_update(job_id,
            status='done',
            progress=100,
            files=all_files + ([zip_path] if zip_path else []),
            file_names=[os.path.basename(f) for f in all_files],
            zip=zip_path,
        )
        cleanup_job(job_id)

    except Exception as e:
        job_update(job_id, status='error', error=str(e))

# ── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/fetch-playlist', methods=['POST'])
def fetch_playlist():
    data = request.get_json()
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if 'soundcloud.com' not in url:
        return jsonify({'error': 'Only SoundCloud URLs are supported'}), 400

    job_id = str(uuid.uuid4())
    job_set(job_id, {'type': 'info', 'status': 'queued', 'error': None, 'playlist_data': None})
    threading.Thread(target=run_info, args=(job_id, url), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json()
    urls = data.get('urls') or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if u and 'soundcloud.com' in u]
    if not urls:
        return jsonify({'error': 'No valid SoundCloud URLs provided'}), 400

    quality = data.get('quality', '192')
    fmt = data.get('format', 'mp3')
    job_id = str(uuid.uuid4())
    job_set(job_id, {
        'status': 'queued', 'progress': 0, 'current_file': '',
        'files': [], 'file_names': [], 'error': None, 'zip': None,
    })
    threading.Thread(target=run_download, args=(job_id, urls, quality, fmt), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/status/<job_id>')
def job_status(job_id):
    job = job_get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('type') == 'info':
        return jsonify({
            'type': 'info',
            'status': job['status'],
            'error': job.get('error'),
            'playlist_data': job.get('playlist_data'),
        })

    return jsonify({
        'type': 'download',
        'status': job['status'],
        'progress': job.get('progress', 0),
        'current_file': job.get('current_file', ''),
        'file_names': job.get('file_names', []),
        'has_zip': bool(job.get('zip')),
        'error': job.get('error'),
    })

@app.route('/api/download-zip/<job_id>')
def download_zip(job_id):
    job = job_get(job_id)
    if not job or not job.get('zip'):
        return jsonify({'error': 'No zip available'}), 404
    return send_file(job['zip'], as_attachment=True, download_name='soundcloud_playlist.zip')

@app.route('/api/download-file/<job_id>/<int:file_index>')
def download_file(job_id, file_index):
    job = job_get(job_id)
    if not job or file_index >= len(job.get('files', [])):
        return jsonify({'error': 'File not found'}), 404
    filepath = job['files'][file_index]
    if not os.path.exists(filepath):
        return jsonify({'error': 'File expired'}), 404
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
PYEOF