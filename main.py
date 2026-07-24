import os
import sys
import time
import shutil
import re
import subprocess
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from mutagen.id3 import ID3, APIC, ID3NoHeaderError, TIT2, TPE1
import base64

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

def get_itunes_info(search_query: str):
    """Fetches clean artist name, track title, and 1000x1000 artwork from iTunes API."""
    artist_name = "Unknown Artist"
    song_title = search_query.title()
    cover_bytes = None

    try:
        url = f"https://itunes.apple.com/search?term={requests.utils.quote(search_query)}&entity=song&limit=1"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("resultCount", 0) > 0:
                track = data["results"][0]
                artist_name = track.get("artistName", artist_name)
                song_title = track.get("trackName", song_title)
                art_url = track.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
                if art_url:
                    img_res = requests.get(art_url, timeout=5)
                    if img_res.status_code == 200:
                        cover_bytes = img_res.content
    except Exception as e:
        print(f"iTunes API Notice: {e}", flush=True)

    return song_title, artist_name, cover_bytes

def download_with_ytdlp(search_query: str, temp_dir: str):
    """
    Downloads high-res audio via yt-dlp.
    Cycles player clients (web, tv_embedded, android_music) to bypass JS decipher errors.
    """
    output_template = os.path.join(temp_dir, "downloaded_track.%(ext)s")
    cookie_path = os.path.join(temp_dir, "youtube_cookies.txt")
    
    b64_cookies = os.getenv("YOUTUBE_COOKIES_B64")
    has_cookies = False

    if b64_cookies:
        try:
            decoded_bytes = base64.b64decode(b64_cookies.strip())
            with open(cookie_path, "wb") as f:
                f.write(decoded_bytes)
            
            has_cookies = True
            print(f"Successfully decoded Base64 cookie file to disk! ({os.path.getsize(cookie_path)} bytes)", flush=True)
        except Exception as cookie_err:
            print(f"Base64 cookie decoding error: {cookie_err}", flush=True)

    # Client strategies to cycle through
    client_strategies = [
        "youtube:player_client=web",
        "youtube:player_client=tv_embedded",
        "youtube:player_client=android_music,mweb"
    ]

    for idx, strategy in enumerate(client_strategies, 1):
        print(f"--- [YT-DLP] Trying strategy {idx}: {strategy} ---", flush=True)
        
        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            f"ytsearch1:{search_query}",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", output_template,
            "--no-playlist",
            "--extractor-args", strategy
        ]

        if has_cookies and "web" in strategy:
            ytdlp_cmd.extend(["--cookies", cookie_path])

        print(f"Executing command: {' '.join(ytdlp_cmd)}", flush=True)
        res = subprocess.run(ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)

        if res.stdout:
            print(f"[yt-dlp stdout]:\n{res.stdout.strip()}", flush=True)
        if res.stderr:
            print(f"[yt-dlp stderr]:\n{res.stderr.strip()}", flush=True)

        mp3_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if res.returncode == 0 and mp3_files:
            print(f"yt-dlp successfully downloaded audio stream using strategy {idx}!", flush=True)
            return True

    return False
def run_media_download_background(search_query: str, temp_dir: str, audio_path: str, file_id: str):
    """Background task to run download, apply metadata, and save to cache."""
    failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
    
    try:
        print(f"--- [START] Processing query: '{search_query}' ---", flush=True)
        
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

        # 1. Fetch clean metadata & artwork via iTunes API
        song_title, artist_name, cover_bytes = get_itunes_info(search_query)
        print(f"iTunes Metadata -> Track: '{song_title}', Artist: '{artist_name}'", flush=True)

        cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")
        if cover_bytes:
            with open(cover_output_path, "wb") as f:
                f.write(cover_bytes)
            print("Saved 1000x1000 iTunes cover art to cache!", flush=True)

        # 2. Build search query
        yt_search_term = f"{artist_name} {song_title} audio" if artist_name != "Unknown Artist" else search_query

        # 3. Run download engine
        download_success = download_with_ytdlp(yt_search_term, temp_dir)

        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not download_success or not downloaded_files:
            print("--- [ERROR] Download engine failed ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download engine failed")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
        
        # 4. Write ID3 tags directly to MP3
        try:
            try:
                tags = ID3(downloaded_mp3_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=song_title))
            tags.add(TPE1(encoding=3, text=artist_name))
            tags.save(downloaded_mp3_path)
            print("Successfully embedded clean ID3 tags into MP3!", flush=True)
        except Exception as e:
            print(f"ID3 write notice: {e}", flush=True)

        # 5. Move finished MP3 to primary cache directory
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
            
            song_title = query.title()
            artist_name = "Unknown Artist"
            try:
                tags = ID3(audio_path)
                song_title = str(tags.get("TIT2", query.title()))
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
        
        # 2. Always wipe stale failed markers on new incoming requests
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

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
