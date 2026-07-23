import os
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# 1. Create a directory to store the downloaded songs on your server
os.makedirs("music_cache", exist_ok=True)

# 2. Mount the directory so the files can be accessed via a public URL
app.mount("/audio", StaticFiles(directory="music_cache"), name="audio")

@app.get("/download-song")
def download_song(spotify_url: str):
    """
    Takes a Spotify URL, downloads the MP3, and returns a playable link.
    """
    try:
        # Extract the unique Spotify track ID from the URL to use as the filename
        # Example URL: https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT
        track_id = spotify_url.split("/")[-1].split("?")[0]
        
        file_name = f"{track_id}.mp3"
        file_path = f"music_cache/{file_name}"
        
        # If we already downloaded this song previously, skip the download to save time!
        if not os.path.exists(file_path):
            # Run spotDL command via Python's subprocess
            subprocess.run([
                "spotdl", 
                "download", 
                spotify_url, 
                "--format", "mp3", 
                "--output", f"music_cache/{{track-id}}.{{ext}}"
            ], check=True)
        
        # Return the public URL where Thunkable can play the audio
        # Note: In Render, it uses HTTPS automatically. Just replace the domain!
        return {
            "status": "ready",
            "audio_url": f"https://your-api-name.onrender.com/audio/{file_name}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
