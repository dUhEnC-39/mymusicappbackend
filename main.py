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
            # Matches both watch?v= and watch%3Fv%3D
            video_ids = re.findall(r'(?:watch\?v=|watch%3Fv%3D)([a-zA-Z0-9_-]{11})', res.text)
            if video_ids:
                direct_url = f"https://www.youtube.com/watch?v={video_ids[0]}"
                print(f"--- [DDG SUCCESS] Found direct URL: {direct_url} ---", flush=True)
                return direct_url
    except Exception as e:
        print(f"DuckDuckGo search notice: {e}", flush=True)
        
    return None

def download_with_ytdlp(search_query: str, temp_dir: str):
    """Downloads audio + metadata + thumbnail using yt-dlp with Safari web client spoofing."""
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
        "--add-metadata",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--extractor-args", "youtube:player_client=web_safari,mweb"
    ]
    
    print(f"Executing yt-dlp: {' '.join(ytdlp_cmd)}", flush=True)
    res = subprocess.run(ytdlp_cmd, stdout=None, stderr=None, timeout=60)
    
    mp3_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
    if res.returncode == 0 and mp3_files:
        print("yt-dlp successfully downloaded high-quality audio track!", flush=True)
        return True
    return False

def run_media_download_background(search_query: str, temp_dir: str, audio_path: str, file_id: str):
    """Downloads audio, extracts/formats metadata, and manages cover art."""
    failed_marker = os.path.join(CACHE_DIR, f"{file_id}.failed")
    
    try:
        print(f"--- [START] Processing query: '{search_query}' ---", flush=True)
        
        # Always remove stale failed marker before starting
        if os.path.exists(failed_marker):
            os.remove(failed_marker)

        spotdl_cache_folder = os.path.expanduser("~/.spotdl")
        if os.path.exists(spotdl_cache_folder):
            try:
                shutil.rmtree(spotdl_cache_folder, ignore_errors=True)
            except Exception:
                pass

        download_success = download_with_ytdlp(search_query, temp_dir)

        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not download_success or not downloaded_files:
            print("--- [ERROR] Download engine failed to produce an MP3 ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download engines failed")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])
        print(f"Downloaded MP3 ready: {downloaded_files[0]}", flush=True)
        
        # --- METADATA FORMATTING ---
        artist_name = "Unknown Artist"
        song_title = search_query.title()

        try:
            tags = ID3(downloaded_mp3_path)
            extracted_artist = str(tags.get("TPE1", "")).strip()
            extracted_title = str(tags.get("TIT2", "")).strip()
            
            if extracted_artist and extracted_artist != "Unknown Artist":
                artist_name = extracted_artist
            if extracted_title and extracted_title != "Unknown Title":
                song_title = extracted_title
        except (ID3NoHeaderError, Exception) as tag_err:
            print(f"Metadata tag notice: {tag_err}", flush=True)

        # Fallback parsing if metadata tags are generic
        if artist_name == "Unknown Artist" and " - " in song_title:
            parts = song_title.split(" - ", 1)
            artist_name = parts[0].strip()
            song_title = parts[1].strip()
        elif artist_name == "Unknown Artist" and len(search_query.split()) >= 2:
            words = search_query.title().split()
            artist_name = words[-1]
            song_title = " ".join(words[:-1])

        print(f"Final Metadata -> Song: '{song_title}', Artist: '{artist_name}'", flush=True)

        # Write clean metadata back to MP3 ID3 tags
        try:
            try:
                tags = ID3(downloaded_mp3_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=song_title))
            tags.add(TPE1(encoding=3, text=artist_name))
            tags.save(downloaded_mp3_path)
        except Exception as e:
            print(f"ID3 rewrite notice: {e}", flush=True)

        # --- COVER ART HANDLING ---
        cover_output_path = os.path.join(CACHE_DIR, f"{file_id}.jpg")
        
        # 1. Try SACAD for 1000x1000 cover art
        try:
            sacad_cmd = [
                sys.executable, "-m", "sacad", 
                artist_name, 
                song_title, 
                "1000", 
                cover_output_path
            ]
            print(f"Running SACAD artwork search for '{artist_name}' - '{song_title}'...", flush=True)
            sacad_res = subprocess.run(sacad_cmd, stdout=None, stderr=None, timeout=20)
            if sacad_res.returncode == 0 and os.path.exists(cover_output_path):
                print("SACAD successfully downloaded 1000x1000 cover art!", flush=True)
        except Exception as sacad_err:
            print(f"SACAD notice: {sacad_err}", flush=True)

        # 2. Fallback: Copy YouTube thumbnail
        if not os.path.exists(cover_output_path):
            jpg_files = [f for f in os.listdir(temp_dir) if f.endswith(".jpg") or f.endswith(".webp")]
            if jpg_files:
                thumb_path = os.path.join(temp_dir, jpg_files[0])
                shutil.copy(thumb_path, cover_output_path)
                print("Fallback: Using YouTube video thumbnail for cover art!", flush=True)

        # 3. Fallback: Extract embedded cover art from MP3
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

        # Move final MP3 to primary cache directory
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
def download_song(song: str, background_tasks: BackgroundTasks, request: Request, retry: bool = False):
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
        
        # Force clear failed marker on new attempts
        if retry and os.path.exists(failed_marker):
            os.remove(failed_marker)
            
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
        
        # 2. If marked failed previously and retry is False, instruct client
        if os.path.exists(failed_marker) and not retry:
            os.remove(failed_marker)  # Auto-clear on new requests

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
