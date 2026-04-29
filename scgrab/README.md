# SCGrab — SoundCloud Playlist Downloader

A web app to download SoundCloud tracks and playlists as MP3 or original audio.

## Project Structure

```
scgrab/
├── app.py              # Flask backend
├── templates/
│   └── index.html      # Frontend UI
├── requirements.txt
├── render.yaml         # Render deployment config
└── downloads/          # Temp download folder (auto-created)
```

## Deploy to Render (Free)

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   gh repo create scgrab --public --push
   ```

2. **Create Render Web Service**
   - Go to [render.com](https://render.com) → New → Web Service
   - Connect your GitHub repo
   - Render will auto-detect `render.yaml` and configure everything

3. **That's it!** Render will install dependencies and start the server.

## Run Locally

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

## Notes

- Downloaded files are automatically deleted after **5 minutes**
- Render's free tier may spin down after inactivity (first request takes ~30s to wake)
- For heavy usage, upgrade to Render's paid tier or use a persistent disk
- FFmpeg is required for MP3 conversion — Render's default environment includes it
