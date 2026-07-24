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

def get_youtube_url_from_duckduckgo(search_query: str):
    """
    Searches DuckDuckGo HTML to resolve direct YouTube URLs.
    Handles both raw (watch?v=) and URL-encoded (watch%3Fv%3D) link structures.
    """
    print(f"--- [DDG SEARCH] Resolving direct YouTube URL for '{search_query}' ---", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    encoded_query = requests.utils.quote(f"site:youtube.com/watch {search_query}")
    search_url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
    
    try:
        res = requests.get(search_url, headers=headers, timeout=10)
        if res.status_code == 200:
            video_ids = re.findall(r'(?:watch\?v=|watch%3Fv%3D)([a-zA-Z0-9_-]{11})', res.text)
            if video_ids:
                direct_url = f"https://www.youtube.com/watch?v={video_ids[0]}"
                print(f"--- [DDG SUCCESS] Found direct URL: {direct_url} ---", flush=True)
                return direct_url
    except Exception as e:
        print(f"DuckDuckGo search notice: {e}", flush=True)
        
    return None

def download_with_ytdlp(search_query: str, temp_dir: str):
    """
    Downloads audio using the exact proven yt-dlp configuration.
    Zero risky CLI flags added.
    """
    target_url = get_youtube_url_from_duckduckgo(search_query)
    if not target_url:
        target_url = f"ytsearch1:{search_query}"
        print(f"--- [FALLBACK] Using native ytsearch1 for '{search_query}' ---", flush=True)

    output_template = os.path.join(temp_dir, "downloaded_track.%(ext)s")
    
    ytdlp_cmd = [
        sys.executable, "-m", "yt_dlp",
        target_url,
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", output_template,
        "--no-playlist",
        "--extractor-args", "youtube:player_client=android,mweb"
    ]
    
    print(f"Executing yt-dlp: {' '.join(ytdlp_cmd)}", flush=True)
    res = subprocess.run(ytdlp_cmd, stdout=None, stderr=None, timeout=60)
    
    mp3_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
    if res.returncode == 0 and mp3_files:
        print("yt-dlp successfully downloaded high-quality audio track!", flush=True)
        return True
    return False

def enrich_metadata_and_cover(search_query: str, downloaded_mp3_path: str, file_id: str):
    """
    Fetches clean Artist, Song Title, and 1000x1000 Album Cover via iTunes API
    completely independently of yt-dlp.
    """
    artist_name = "Unknown Artist"
    song_title = search_query.title()
    cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")

    # 1. Fetch official track details & 1000x1000 artwork from iTunes API
    try:
        itunes_url = f"https://itunes.apple.com/search?term={requests.utils.quote(search_query)}&entity=song&limit=1"
        res = requests.get(itunes_url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("resultCount", 0) > 0:
                track = data["results"][0]
                artist_name = track.get("artistName", artist_name)
                song_title = track.get("trackName", song_title)
                
                artwork_url = track.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
                if artwork_url:
                    img_res = requests.get(artwork_url, timeout=10)
                    if img_res.status_code == 200:
                        with open(cover_output_path, "wb") as f:
                            f.write(img_res.content)
                        print("Successfully saved 1000x1000 artwork from iTunes!", flush=True)
    except Exception as e:
        print(f"iTunes API notice: {e}", flush=True)

    # 2. Fallback SACAD search if iTunes missed artwork
    if not os.path.exists(cover_output_path):
        try:
            sacad_cmd = [
                sys.executable, "-m", "sacad", 
                artist_name, 
                song_title, 
                "1000", 
                cover_output_path
            ]
            print(f"Running SACAD artwork search for '{artist_name}' - '{song_title}'...", flush=True)
            sacad_res = subprocess.run(sacad_cmd, stdout=None, stderr=None, timeout=15)
            if sacad_res.returncode == 0 and os.path.exists(cover_output_path):
                print("SACAD successfully downloaded cover art!", flush=True)
        except Exception as sacad_err:
            print(f"SACAD notice: {sacad_err}", flush=True)

    # 3. Fallback title/artist parsing if iTunes missed
    if artist_name == "Unknown Artist":
        if " - " in search_query:
            parts = search_query.split(" - ", 1)
            artist_name = parts[0].strip().title()
            song_title = parts[1].strip().title()
        elif len(search_query.split()) >= 2:
            words = search_query.title().split()
            artist_name = words[-1]
            song_title = " ".join(words[:-1])

    # 4. Write clean ID3 tags directly into the MP3 file
    try:
        try:
            tags = ID3(downloaded_mp3_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=song_title))
        tags.add(TPE1(encoding=3, text=artist_name))
        tags.save(downloaded_mp3_path)
    except Exception as e:
        print(f"ID3 write notice: {e}", flush=True)

    print(f"Final Metadata -> Song: '{song_title}', Artist: '{artist_name}'", flush=True)
    return song_title, artist_name

def run_media_download_background(search_query: str, temp_dir: str, audio_path: str, file_id: str):
    """Background task to run download, apply metadata, and save to cache."""
    failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
    
    try:
        print(f"--- [START] Processing query: '{search_query}' ---", flush=True)
        
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

        spotdl_cache_folder = os.path.expanduser("~/.spotdl")
        if os.path.exists(spotdl_cache_folder):
            try:
                shutil.rmtree(spotdl_cache_folder, ignore_errors=True)
            except Exception:
                pass

        # Execute the working yt-dlp downloader
        download_success = download_with_ytdlp(search_query, temp_dir)

        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not download_success or not downloaded_files:
            print("--- [ERROR] Download engine failed to produce an MP3 ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download engines failed")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
        print(f"Downloaded MP3 ready: {downloaded_files[0]}", flush=True)

        # Enrich Metadata & Cover Art via Python
        enrich_metadata_and_cover(search_query, downloaded_mp3_path, file_id)

        # Move final file to primary cache
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
