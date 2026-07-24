import os
import sys
import time
import shutil
import re
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

# Force Python unbuffered logging so prints show up live in Northflank
os.environ["PYTHONUNBUFFERED"] = "1"

# --- FASTAPI SETUP ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_DIR = "music_cache"
MAX_FILE_AGE_SECONDS = 3600  # 1 hour auto-cleanup

os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")


# --- HELPER FUNCTIONS ---
def cleanup_old_files():
    """Deletes cached MP3s, cover images, and error markers older than 1 hour."""
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
    """Downloads audio with live unbuffered logging and YouTube cloud-block flags."""
    failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
    
    try:
        print(f"--- [START] Processing search query: '{search_query}' ---", flush=True)
        
        # Remove previous failure marker if retrying
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

        # 1. spotDL command configured for cloud server execution
        download_cmd = [
            sys.executable, "-m", "spotdl", 
            "download", 
            search_query,
            "--output-format", "mp3",
            "--audio", "youtube-music"
        ]
        
        print(f"Executing: {' '.join(download_cmd)}", flush=True)
        
        # Using stdout=None and stderr=None streams logs directly to Northflank live!
        result = subprocess.run(
            download_cmd, 
            cwd=temp_dir, 
            stdout=None, 
            stderr=None, 
            timeout=90
        )
        
        if result.returncode != 0:
            print(f"--- [ERROR] spotDL returned exit code {result.returncode} ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write(f"spotdl failed with code {result.returncode}")
            return

        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not downloaded_files:
            print("--- [ERROR] No MP3 generated in temp directory ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("No MP3 file produced")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
        print(f"Downloaded MP3 found: {downloaded_files[0]}", flush=True)
        
        # 2. Extract metadata
        artist_name = "Unknown Artist"
        album_name = "Unknown Album"
        try:
            tags = ID3(downloaded_mp3_path)
            artist_name = str(tags.get("TPE1", "Unknown Artist"))
            album_name = str(tags.get("TALB", "Unknown Album"))
            print(f"Extracted Metadata -> Artist: '{artist_name}', Album: '{album_name}'", flush=True)
        except (ID3NoHeaderError, Exception) as tag_err:
            print(f"Could not read ID3 tags: {tag_err}", flush=True)

        # 3. Fetch 1000x1000 cover art using SACAD
        cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")
        if artist_name != "Unknown Artist" and album_name != "Unknown Album":
            try:
                sacad_cmd = [
                    sys.executable, "-m", "sacad", 
                    artist_name, 
                    album_name, 
                    "1000", 
                    cover_output_path
                ]
                print("Running SACAD for high-res artwork...", flush=True)
                sacad_res = subprocess.run(
                    sacad_cmd, 
                    stdout=None, 
                    stderr=None, 
                    timeout=30
                )
                if sacad_res.returncode == 0 and os.path.exists(cover_output_path):
                    print("SACAD successfully downloaded 1000x1000 cover art!", flush=True)
            except Exception as sacad_err:
                print(f"SACAD error: {sacad_err}", flush=True)

        # Fallback: Extract embedded cover art from MP3
        if not os.path.exists(cover_output_path):
            try:
                tags = ID3(downloaded_mp3_path)
                for tag in tags.values():
                    if isinstance(tag, APIC):
                        with open(cover_output_path, 'wb') as img_file:
                            img_file.write(tag.data)
                        print("Fallback: Extracted embedded cover art from MP3!", flush=True)
                        break
            except Exception as embed_err:
                print(f"Embedded art fallback error: {embed_err}", flush=True)

        # 4. Move final MP3 to cache directory
        shutil.move(downloaded_mp3_path, audio_path)
        print(f"--- [SUCCESS] Saved to {audio_path} ---", flush=True)

    except subprocess.TimeoutExpired:
        print("--- [TIMEOUT] spotDL process timed out after 90s ---", flush=True)
        with open(failed_marker, "w") as f:
            f.write("Timeout expired during download")

    except Exception as e:
        print(f"--- [CRASH] Exception: {e} ---", flush=True)
        with open(failed_marker, "w") as f:
            f.write(str(e))

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

# --- ENDPOINTS ---
@app.get("/download-song")
def download_song(song: str, background_tasks: BackgroundTasks, request: Request):
    background_tasks.add_task(cleanup_old_files)
    
    try:
        query = song.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Song query cannot be empty")

        if "http" in query:
            clean_query = query.split("?")[0]
            file_id = clean_query.split("/")[-1]
        else:
            clean_query = query
            file_id = re.sub(r'[^a-zA-Z0-9]', '_', query.lower())
        
        audio_file_name = f"{file_id}.mp3"
        audio_path = os.path.join(CACHE_DIR, audio_file_name)
        failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
        
        base_url = str(request.base_url).rstrip("/")
        
        # 1. If song is ready in cache
        if os.path.exists(audio_path):
            existing_cover_name = get_existing_cover(file_id)
            cover_url = f"{base_url}/audio/{existing_cover_name}" if existing_cover_name else None
            
            song_title = "Unknown Title"
            artist_name = "Unknown Artist"
            try:
                tags = ID3(audio_path)
                song_title = str(tags.get("TIT2", query))
                artist_name = str(tags.get("TPE1", "Unknown Artist"))
            except Exception:
                pass

            return {
                "status": "ready",
                "audio_url": f"{base_url}/audio/{audio_file_name}",
                "cover_url": cover_url,
                "song_name": song_title,
                "artist": artist_name
            }
        
        # 2. If download previously failed
        if os.path.exists(failed_marker):
            return {
                "status": "failed",
                "message": "Download failed on server. Try a different search term."
            }

        # 3. If currently processing
        temp_dir = os.path.join(CACHE_DIR, f"temp_{file_id}")
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
            background_tasks.add_task(run_media_download_background, clean_query, temp_dir, audio_path, file_id)
            
        return {
            "status": "processing",
            "message": "Song and cover art are processing. Try again in 10-15 seconds."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
