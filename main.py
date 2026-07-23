import os
import time
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Configuration
CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour (60 minutes * 60 seconds)

# Create cache directory and mount it so Thunkable can access the MP3s
os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")

def cleanup_old_files():
    """
    Scans the music directory and deletes files older than MAX_FILE_AGE_SECONDS.
    This keeps your free Render server from running out of storage space.
    """
    now = time.time()
    for filename in os.listdir(CACHE_DIR):
        file_path = os.path.join(CACHE_DIR, filename)
        
        # Make sure it is a file, not a sub-directory
        if os.path.isfile(file_path):
            file_age = now - os.path.getmtime(file_path)
            
            # If the file is older than our limit, delete it
            if file_age > MAX_FILE_AGE_SECONDS:
                try:
                    os.remove(file_path)
                    print(f"Deleted old file: {filename}")
                except Exception as e:
                    # If the file is currently being read by someone, skip it for now
                    print(f"Could not delete {filename}: {e}")

@app.get("/download-song")
def download_song(spotify_url: str, background_tasks: BackgroundTasks):
    """
    Takes a Spotify URL, downloads the MP3, and returns a playable link.
    Also triggers a background cleanup of old files.
    """
    background_tasks.add_task(cleanup_old_files)
    
    try:
        # 1. Clean the URL
        clean_url = spotify_url.split("?")[0]
        
        # 2. Extract the unique track ID 
        track_id = clean_url.split("/")[-1]
        
        file_name = f"{track_id}.mp3"
        file_path = os.path.join(CACHE_DIR, file_name)
        
        # 3. Only run the download if it's not already in the cache
        if not os.path.exists(file_path):
            # Give spotDL exactly what it wants: the {track-id} variable!
            subprocess.run([
                "spotdl", 
                "download", 
                clean_url, 
                "--format", "mp3", 
                "--output", f"{CACHE_DIR}/{{track-id}}.mp3"
            ], check=True)
        
        # Return the public URL
        return {
            "status": "ready",
            "audio_url": f"https://mymusicappbackend.onrender.com/audio/{file_name}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
