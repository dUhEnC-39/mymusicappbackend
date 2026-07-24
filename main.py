import os
import time
import shutil
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles

app = FastAPI()

CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour

os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")

def cleanup_old_files():
    now = time.time()
    for filename in os.listdir(CACHE_DIR):
        file_path = os.path.join(CACHE_DIR, filename)
        if os.path.isfile(file_path):
            if (now - os.path.getmtime(file_path)) > MAX_FILE_AGE_SECONDS:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

def run_spotdl_background(clean_url: str, temp_dir: str, file_path: str):
    """Downloads song in the background without blocking the API response."""
    try:
        subprocess.run(["spotdl", "download", clean_url], cwd=temp_dir, check=True)
        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if downloaded_files:
            shutil.move(os.path.join(temp_dir, downloaded_files[0]), file_path)
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

@app.get("/download-song")
def download_song(spotify_url: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(cleanup_old_files)
    
    try:
        clean_url = spotify_url.split("?")[0]
        track_id = clean_url.split("/")[-1]
        
        file_name = f"{track_id}.mp3"
        file_path = os.path.join(CACHE_DIR, file_name)
        
        # Check if file is already cached and ready
        if os.path.exists(file_path):
            return {
                "status": "ready",
                "audio_url": f"https://mymusicappbackend.onrender.com/audio/{file_name}"
            }
        
        temp_dir = os.path.join(CACHE_DIR, f"temp_{track_id}")
        
        # If download isn't already running, kick it off in the background
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
            background_tasks.add_task(run_spotdl_background, clean_url, temp_dir, file_path)
        
        # Respond immediately so Thunkable doesn't time out!
        return {
            "status": "processing",
            "message": "Song is downloading. Try again in 15 seconds."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
