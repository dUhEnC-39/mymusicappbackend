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

# Force Python unbuffered logging so prints stream live to Northflank logs
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
MAX_FILE_AGE_SECONDS = 3600  # Auto-cleanup files older than 1 hour

os.makedirs(CACHE_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=CACHE_DIR), name="audio")


# --- HELPER FUNCTIONS ---
def cleanup_old_files():
    """Deletes cached MP3s, cover images, and failure markers older than 1 hour."""
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
    """Checks if a cover image exists for this track in the cache."""
    for ext in [".jpg", ".png", ".webp", ".jpeg"]:
        cover_path = os.path.join(CACHE_DIR, f"{file_id}{ext}")
        if os.path.exists(cover_path):
            return f"{file_id}{ext}"
    return None

def download_with_ytdlp(search_query: str, temp_dir: str):
    """Downloads high-res audio using yt-dlp with Node.js runtime, client spoofing, and optional cookies."""
    print(f"--- [HIGH-QUAL ENGINE] Fetching high-res audio via yt-dlp for '{search_query}' ---", flush=True)
    
    output_template = os.path.join(temp_dir, "downloaded_track.%(ext)s")
    cookie_file_path = os.path.join(temp_dir, "youtube_cookies.txt")
    
    # 1. Check for YOUTUBE_COOKIES in Northflank environment variables
    yt_cookies_data = os.getenv("YOUTUBE_COOKIES")
    use_cookies = False
    
    if yt_cookies_data:
        try:
            with open(cookie_file_path, "w", encoding="utf-8") as f:
                f.write(yt_cookies_data.strip())
            use_cookies = True
            print("Successfully loaded YouTube authentication cookies.", flush=True)
        except Exception as cookie_err:
            print(f"Cookie notice: {cookie_err}", flush=True)

    # 2. Build yt-dlp command with client spoofing & Node.js solver
    ytdlp_cmd = [
        sys.executable, "-m", "yt_dlp",
        f"ytsearch1:{search_query} audio",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",  # Highest VBR quality (~250-320 kbps)
        "-o", output_template,
        "--no-playlist",
        "--js-runtimes", "node",  # Solves YouTube JS challenges using Node.js
        "--extractor-args", "youtube:player_client=ios,tv_embedded"
    ]

    # Attach cookies flag if available
    if use_cookies:
        ytdlp_cmd.extend(["--cookies", cookie_file_path])
    
    print(f"Executing yt-dlp: {' '.join(ytdlp_cmd)}", flush=True)
    res = subprocess.run(ytdlp_cmd, stdout=None, stderr=None, timeout=60)
    
    if res.returncode == 0 and any(f.endswith(".mp3") for f in os.listdir(temp_dir)):
        print("yt-dlp successfully downloaded high-quality audio stream!", flush=True)
        return True

    return False

def run_media_download_background(search_query: str, temp_dir: str, audio_path: str, file_id: str):
    """Downloads audio, extracts metadata, and fetches 1000x1000 cover art."""
    failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
    
    try:
        print(f"--- [START] Processing query: '{search_query}' ---", flush=True)
        
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

        # 1. Clear out stale spotDL cache directory
        spotdl_cache_folder = os.path.expanduser("~/.spotdl")
        if os.path.exists(spotdl_cache_folder):
            try:
                shutil.rmtree(spotdl_cache_folder, ignore_errors=True)
            except Exception:
                pass

        download_success = False

        # 2. Try spotDL with a strict 10-second timeout
        download_cmd = [
            sys.executable, "-m", "spotdl", 
            search_query,
            "--output-format", "mp3"
        ]

        print(f"Executing spotDL: {' '.join(download_cmd)}", flush=True)
        
        try:
            result = subprocess.run(
                download_cmd, 
                cwd=temp_dir, 
                stdout=None, 
                stderr=None, 
                timeout=10
            )
            if result.returncode == 0 and any(f.endswith(".mp3") for f in os.listdir(temp_dir)):
                download_success = True
                print("spotDL download successful!", flush=True)
        except Exception:
            print("spotDL hit limit or timed out. Switching to yt-dlp engine...", flush=True)

        # 3. Fallback to anti-bot yt-dlp engine
        if not download_success:
            download_success = download_with_ytdlp(search_query, temp_dir)

        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not download_success or not downloaded_files:
            print("--- [ERROR] All download engines failed to produce an MP3 ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download engines failed")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
        print(f"Downloaded MP3 ready: {downloaded_files[0]}", flush=True)
        
        # 4. Extract metadata
        artist_name = "Unknown Artist"
        album_name = "Unknown Album"
        try:
            tags = ID3(downloaded_mp3_path)
            artist_name = str(tags.get("TPE1", "Unknown Artist"))
            album_name = str(tags.get("TALB", "Unknown Album"))
            print(f"Extracted Metadata -> Artist: '{artist_name}', Album: '{album_name}'", flush=True)
        except (ID3NoHeaderError, Exception) as tag_err:
            print(f"Metadata tag notice: {tag_err}", flush=True)

        # 5. Fetch 1000x1000 cover art using SACAD
        cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")
        
        search_parts = search_query.split(" ")
        query_artist = search_parts[-1] if len(search_parts) > 1 else search_query
        query_album = search_query

        sacad_artist = artist_name if artist_name != "Unknown Artist" else query_artist
        sacad_album = album_name if album_name != "Unknown Album" else query_album

        try:
            sacad_cmd = [
                sys.executable, "-m", "sacad", 
                sacad_artist, 
                sacad_album, 
                "1000", 
                cover_output_path
            ]
            print(f"Running SACAD artwork search for '{sacad_artist}' - '{sacad_album}'...", flush=True)
            sacad_res = subprocess.run(sacad_cmd, stdout=None, stderr=None, timeout=25)
            if sacad_res.returncode == 0 and os.path.exists(cover_output_path):
                print("SACAD successfully downloaded 1000x1000 cover art!", flush=True)
        except Exception as sacad_err:
            print(f"SACAD notice: {sacad_err}", flush=True)

        # Fallback: Extract embedded cover art from MP3 if SACAD missed
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
                print(f"Embedded art fallback notice: {embed_err}", flush=True)

        # 6. Move final MP3 to primary cache directory
        shutil.move(downloaded_mp3_path, audio_path)
        print(f"--- [SUCCESS] Song processing complete! Saved to {audio_path} ---", flush=True)

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
        
        # 1. Return cached audio if available
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
        
        # 2. Return failed state if download previously errored
        if os.path.exists(failed_marker):
            return {
                "status": "failed",
                "message": "Download failed on server. Try a different search term."
            }

        # 3. Queue download task
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
