import os
import time
import shutil
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Configuration
CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour

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
            
            if file_age > MAX_FILE_AGE_SECONDS:
                try:
                    os.remove(file_path)
                    print(f"Deleted old file: {filename}")
                except Exception as e:
                    pass

@app.get("/download-song")
def download_song(spotify_url: str, background_tasks: BackgroundTasks):
    """
    Takes a Spotify URL, downloads the MP3, and returns a playable link.
    """
    background_tasks.add_task(cleanup_old_files)
    
    try:
        clean_url = spotify_url.split("?")[0]
        track_id = clean_url.split("/")[-1]
        
        file_name = f"{track_id}.mp3"
        file_path = os.path.join(CACHE_DIR, file_name)
        
        if not os.path.exists(file_path):
            # Create a unique temporary folder just for this one download
            temp_dir = os.path.join(CACHE_DIR, f"temp_{track_id}")
            os.makedirs(temp_dir, exist_ok=True)
            
            try:
                # Run spotDL inside the temp folder! No output flags needed.
                subprocess.run([
                    "spotdl", 
                    clean_url, 
                    "--format", "mp3"
                ], cwd=temp_dir, check=True)
                
                # Look inside the temp folder for the MP3 spotDL just created
                downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
                
                if not downloaded_files:
                    raise Exception("spotDL finished, but no MP3 file was found.")
                
                temp_file_path = os.path.join(temp_dir, downloaded_files[0])
                
                # Move the file to our main cache and rename it to our strict track-id
                shutil.move(temp_file_path, file_path)
                
            finally:
                # Always delete the temporary folder when we are done, even if it crashed
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
        
        return {
            "status": "ready",
            "audio_url": f"https://mymusicappbackend.onrender.com/audio/{file_name}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
