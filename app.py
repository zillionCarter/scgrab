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

# In-memory job store { job_id: { status, progress, files, error } }
jobs = {}


def cleanup_job(job_id, delay=300):
    """Delete downloaded files after delay seconds."""
    def _cleanup():
        time.sleep(delay)
        job = jobs.get(job_id, {})
        for f in job.get('files', []):
            try:
                os.remove(f)
            except Exception:
                pass
        # Also clean up job directory
        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
        try:
            if os.path.isdir(job_dir):
                import shutil
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
        jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


def get_proxy():
    return os.environ.get('PROXY_URL') or None


def run_download(job_id, urls, quality, fmt):
    job = jobs[job_id]
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    downloaded_files = []

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = int(downloaded / total * 100) if total else 0
            filename = d.get('filename', '')
            job['progress'] = percent
            job['current_file'] = os.path.basename(filename)
        elif d['status'] == 'finished':
            filepath = d.get('filename', '')
            if filepath and os.path.exists(filepath):
                downloaded_files.append(filepath)

    base_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(job_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'ignoreerrors': True,
        'retries': 3,
        'fragment_retries': 3,
    }

    proxy = get_proxy()
    if proxy:
        base_opts['proxy'] = proxy

    if fmt in ['mp3', 'wav']:
        base_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': fmt,
            'preferredquality': quality,
        }]

    try:
        job['status'] = 'downloading'
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            ydl.download(urls)

        # Collect all files in job_dir
        all_files = [
            os.path.join(job_dir, f)
            for f in os.listdir(job_dir)
            if os.path.isfile(os.path.join(job_dir, f))
        ]

        # Catch silent failures — ignoreerrors means no exception is raised
        if not all_files:
            job['status'] = 'error'
            job['error'] = (
                'No files were downloaded. SoundCloud may be geo-blocking this server. '
                'Set the PROXY_URL environment variable to a working proxy and try again.'
            )
            return

        job['files'] = all_files
        job['file_names'] = [os.path.basename(f) for f in all_files]
        job['status'] = 'done'
        job['progress'] = 100

        # Zip multiple files
        if len(all_files) > 1:
            zip_path = os.path.join(DOWNLOAD_DIR, f'{job_id}.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in all_files:
                    zf.write(f, os.path.basename(f))
            job['zip'] = zip_path
            job['files'].append(zip_path)

        cleanup_job(job_id)

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)


def run_info(job_id, url):
    job = jobs[job_id]
    ydl_opts = {
        'extract_flat': False,
        'quiet': True,
        'ignoreerrors': True,
        'retries': 3,
    }
    proxy = get_proxy()
    if proxy:
        ydl_opts['proxy'] = proxy

    try:
        job['status'] = 'downloading'
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Catch silent failure — ignoreerrors returns None instead of raising
        if info is None:
            job['status'] = 'error'
            job['error'] = (
                'Could not fetch playlist info. SoundCloud may be geo-blocking this server. '
                'Set the PROXY_URL environment variable and try again.'
            )
            return

        playlist_title = info.get('title') or 'SoundCloud Audio'
        uploader = info.get('uploader') or info.get('channel') or ''
        thumbnail = info.get('thumbnail') or ''

        tracks = []
        if 'entries' in info:
            for t in (info['entries'] or []):
                if not t:
                    continue
                t_url = t.get('webpage_url') or t.get('url')
                if t_url:
                    tracks.append({
                        'title': t.get('title') or 'Unknown Track',
                        'url': t_url,
                        'uploader': t.get('uploader') or uploader,
                        'duration': t.get('duration'),
                        'thumbnail': t.get('thumbnail') or thumbnail,
                    })
        else:
            tracks.append({
                'title': playlist_title,
                'url': info.get('webpage_url') or url,
                'uploader': uploader,
                'duration': info.get('duration'),
                'thumbnail': thumbnail,
            })

        job['playlist_data'] = {
            'title': playlist_title,
            'uploader': uploader,
            'count': len(tracks),
            'thumbnail': thumbnail,
            'tracks': tracks,
        }
        job['status'] = 'done'

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)


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
    jobs[job_id] = {
        'type': 'info',
        'status': 'queued',
        'progress': 0,
        'current_file': 'Fetching metadata...',
        'file_names': [],
        'error': None,
        'playlist_data': None,
    }

    threading.Thread(target=run_info, args=(job_id, url), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json()
    urls = data.get('urls')

    if not urls or not isinstance(urls, list):
        url = data.get('url')
        urls = [url] if url else []

    urls = [u.strip() for u in urls if u and 'soundcloud.com' in u]
    if not urls:
        return jsonify({'error': 'Only valid SoundCloud URLs are supported'}), 400

    quality = data.get('quality', '192')
    fmt = data.get('format', 'mp3')

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'queued',
        'progress': 0,
        'current_file': '',
        'files': [],
        'file_names': [],
        'error': None,
        'zip': None,
    }

    threading.Thread(target=run_download, args=(job_id, urls, quality, fmt), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/status/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('type') == 'info':
        return jsonify({
            'type': 'info',
            'status': job['status'],
            'progress': job.get('progress', 0),
            'current_file': job.get('current_file', ''),
            'file_names': [],
            'error': job.get('error'),
            'playlist_data': job.get('playlist_data'),
        })

    return jsonify({
        'type': 'download',
        'status': job['status'],
        'progress': job['progress'],
        'current_file': job.get('current_file', ''),
        'file_names': job.get('file_names', []),
        'has_zip': bool(job.get('zip')),
        'error': job.get('error'),
    })


@app.route('/api/download-zip/<job_id>')
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job or not job.get('zip'):
        return jsonify({'error': 'No zip available'}), 404
    return send_file(job['zip'], as_attachment=True, download_name='soundcloud_playlist.zip')


@app.route('/api/download-file/<job_id>/<int:file_index>')
def download_file(job_id, file_index):
    job = jobs.get(job_id)
    if not job or file_index >= len(job.get('files', [])):
        return jsonify({'error': 'File not found'}), 404
    filepath = job['files'][file_index]
    if not os.path.exists(filepath):
        return jsonify({'error': 'File no longer available'}), 404
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
