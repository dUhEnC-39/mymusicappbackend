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


# List of public Piped API mirrors to bypass datacenter IP bans
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.private.coffee",
    "https://pipedapi.mha.fi",
    "https://piped-api.garudalinux.org"
]


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

def download_via_piped_proxy(search_query: str, temp_dir: str):
    """Fetches YouTube audio via Piped Proxy API to completely bypass cloud IP bot blocks."""
    print(f"--- [PROXY ENGINE] Searching Piped Proxy for '{search_query}' ---", flush=True)
    
    for api_base in PIPED_INSTANCES:
        try:
            # 1. Search for song on Piped
            search_url = f"{api_base}/search?q={requests.utils.quote(search_query)}&filter=music_songs"
            res = requests.get(search_url, timeout=10)
            if res.status_code != 200:
                continue
            
            items = res.json().get("items", [])
            if not items:
                continue
            
            video_id = items[0]["url"].replace("/watch?v=", "")
            print(f"Found YouTube Video ID: {video_id} via {api_base}", flush=True)
            
            # 2. Get audio stream details
            stream_res = requests.get(f"{api_base}/streams/{video_id}", timeout=10)
            if stream_res.status_code != 200:
                continue
            
            audio_streams = stream_res.json().get("audioStreams", [])
            if not audio_streams:
                continue
            
            # Sort streams by quality (highest bitrate first)
            audio_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
            direct_audio_url = audio_streams[0]["url"]
            
            # 3. Download direct stream to disk using ffmpeg
            output_mp3_path = os.path.join(temp_dir, "downloaded_track.mp3")
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", direct_audio_url,
                "-vn",
                "-ar", "44100",
                "-ac", "2",
                "-b:a", "192k",
                output_mp3_path
            ]
            
            print("Downloading and converting audio stream via ffmpeg...", flush=True)
            ffmpeg_res = subprocess.run(ffmpeg_cmd, stdout=None, stderr=None, timeout=45)
            
            if ffmpeg_res.returncode == 0 and os.path.exists(output_mp3_path):
                print("Successfully downloaded pristine audio stream via Piped proxy!", flush=True)
                return True
                
        except Exception as e:
            print(f"Piped instance {api_base} failed: {e}", flush=True)
            continue
            
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

        # 2. Try spotDL first with a tight 10-second timeout
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
            print("spotDL hit limit or timed out. Switching to Piped proxy engine...", flush=True)

        # 3. Fallback to Piped Proxy Engine (Bypasses YouTube datacenter IP ban)
        if not download_success:
            download_success = download_via_piped_proxy(search_query, temp_dir)

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
