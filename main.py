import os
import sys
import time
import shutil
import re
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from mutagen.id3 import ID3

app = FastAPI()

# Enable CORS for external requests (e.g., Thunkable)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour

os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")

def cleanup_old_files():
    """Deletes cached MP3s and cover images older than 1 hour."""
    now = time.time()
    for filename in os.listdir(CACHE_DIR):
        file_path = os.path.join(CACHE_DIR, filename)
        if os.path.isfile(file_path):
            if (now - os.path.getmtime(file_path)) > MAX_FILE_AGE_SECONDS:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

def get_existing_cover(file_id: str):
    """Checks if an image file exists for this track in the cache."""
    for ext in [".jpg", ".png", ".webp", ".jpeg"]:
        cover_path = os.path.join(CACHE_DIR, f"{file_id}{ext}")
        if os.path.exists(cover_path):
            return f"{file_id}{ext}"
    return None

def run_media_download_background(search_query: str, temp_dir: str, audio_path: str, file_id: str):
    """Downloads high-quality audio, extracts metadata, and fetches 1000x1000 cover art via SACAD."""
    try:
        # STEP 3: Download audio at 320kbps high quality
        subprocess.run([
            sys.executable, "-m", "spotdl", 
            "download", 
            search_query, 
            "--bitrate", "320k",
            "--ffmpeg-args", "-af volume=2.5"
        ], cwd=temp_dir, check=True)
        
        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if downloaded_files:
            downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
            
            # STEP 1: Extract song name, artist, and album from MP3 ID3 tags into variables
            song_name = "Unknown Title"
            artist_name = "Unknown Artist"
            album_name = "Unknown Album"
            
            try:
                tags = ID3(downloaded_mp3_path)
                song_name = str(tags.get("TIT2", search_query))
                artist_name = str(tags.get("TPE1", "Unknown Artist"))
                album_name = str(tags.get("TALB", "Unknown Album"))
                
                print(f"Metadata Extracted -> Song: '{song_name}', Artist: '{artist_name}', Album: '{album_name}'")
            except Exception as tag_err:
                print(f"Could not read ID3 tags: {tag_err}")

            # STEP 2: Fetch 1000x1000 album cover using SACAD with artist_name and album_name
            cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")
            try:
                subprocess.run([
                    sys.executable, "-m", "sacad", 
                    artist_name, 
                    album_name, 
                    "1000", 
                    cover_output_path
                ], check=False)
                print(f"SACAD completed cover search for '{artist_name}' - '{album_name}'")
            except Exception as sacad_err:
                print(f"SACAD download failed: {sacad_err}")

            # Move final MP3 to primary cache folder
            shutil.move(downloaded_mp3_path, audio_path)

    except Exception as e:
        print(f"Background Process Error: {e}")

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

@app.get("/download-song")
def download_song(song: str, background_tasks: BackgroundTasks, request: Request):
    background_tasks.add_task(cleanup_old_files)
    
    try:
        query = song.strip()
        
        # Sanitize query to generate a clean cached file ID
        if "http" in query:
            clean_query = query.split("?")[0]
            file_id = clean_query.split("/")[-1]
        else:
            clean_query = query
            file_id = re.sub(r'[^a-zA-Z0-9]', '_', query.lower())
        
        audio_file_name = f"{file_id}.mp3"
        audio_path = os.path.join(CACHE_DIR, audio_file_name)
        
        base_url = str(request.base_url).rstrip("/")
        
        # If already downloaded and cached, return audio and cover URLs
        if os.path.exists(audio_path):
            existing_cover_name = get_existing_cover(file_id)
            cover_url = f"{base_url}/audio/{existing_cover_name}" if existing_cover_name else None
            
            return {
                "status": "ready",
                "audio_url": f"{base_url}/audio/{audio_file_name}",
                "cover_url": cover_url
            }
        
        temp_dir = os.path.join(CACHE_DIR, f"temp_{file_id}")
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
            background_tasks.add_task(run_media_download_background, clean_query, temp_dir, audio_path, file_id)
            
        return {
            "status": "processing",
            "message": "Song and 1000x1000 cover art are processing. Try again in 10-15 seconds."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
