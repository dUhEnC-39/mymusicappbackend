import os
import time
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Configuration
CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour (60 minutes * 60 seconds)

# Create cache directory and mount it
os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")

def cleanup_old_files():
    """
    Scans the music directory and deletes files older than MAX_FILE_AGE_SECONDS.
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
        # 1. Clean the URL! This chops off the "?si=" tracking garbage
        clean_url = spotify_url.split("?")[0]
        
        # 2. Extract the unique track ID from the clean URL
        track_id = clean_url.split("/")[-1]
        
        file_name = f"{track_id}.mp3"
        file_path = os.path.join(CACHE_DIR, file_name)
        
        # Only download if it's not already in the cache
        if not os.path.exists(file_path):
            # 3. Run the spotDL command WITHOUT the word "download"
            subprocess.run([
                "spotdl", 
                clean_url, 
                "--format", "mp3", 
                "--output", f"{CACHE_DIR}/{{track-id}}.{{ext}}"
            ], check=True)
        
        # Return the public URL
        return {
            "status": "ready",
            "audio_url": f"https://mymusicappbackend.onrender.com/audio/{file_name}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

