import yt_dlp
ydl=yt_dlp.YoutubeDL({'quiet': True, 'ignoreerrors': True})
try:
    err = ydl.download(['https://soundcloud.com/dj-actinium/sets/set-1'])
    print('NO EXCEPTION, returns:', err)
except Exception as e:
    print('EXCEPTION RAISED:', repr(e))