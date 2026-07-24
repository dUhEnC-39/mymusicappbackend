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

def download_via_proxy_api(search_query: str, output_mp3_path: str):
    """
    Cycles through active Piped and Invidious instances to fetch YouTube audio streams directly.
    """
    print(f"--- [PROXY ENGINE] Searching active instances for '{search_query}' ---", flush=True)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    # Active, high-uptime public API instances
    proxy_instances = [
        # Piped API endpoints
        ("piped", "https://pipedapi.adminforge.de"),
        ("piped", "https://pipedapi.kavin.rocks"),
        ("piped", "https://pipedapi.aston.cx"),
        ("piped", "https://pipedapi.projectsegfau.lt"),
        ("piped", "https://api.piped.private.coffee"),
        
        # Invidious API endpoints
        ("invidious", "https://invidious.nerdvpn.de"),
        ("invidious", "https://inv.tux.pizza"),
        ("invidious", "https://invidious.projectsegfau.lt"),
        ("invidious", "https://invidious.drgns.space")
    ]

    for api_type, api_base in proxy_instances:
        try:
            print(f"Testing {api_type} instance: {api_base}...", flush=True)
            direct_stream_url = None

            if api_type == "piped":
                search_url = f"{api_base}/search?q={requests.utils.quote(search_query)}&filter=music_songs"
                res = requests.get(search_url, headers=headers, timeout=6)
                
                if res.status_code != 200:
                    print(f"[{api_base}] Search HTTP status: {res.status_code}", flush=True)
                    continue

                items = res.json().get("items", [])
                if not items:
                    res = requests.get(f"{api_base}/search?q={requests.utils.quote(search_query)}&filter=all", headers=headers, timeout=6)
                    items = res.json().get("items", []) if res.status_code == 200 else []

                if not items:
                    continue

                video_id = items[0]["url"].replace("/watch?v=", "")
                print(f"Found Video ID '{video_id}' on {api_base}", flush=True)

                stream_res = requests.get(f"{api_base}/streams/{video_id}", headers=headers, timeout=8)
                if stream_res.status_code != 200:
                    print(f"[{api_base}] Stream endpoint returned HTTP {stream_res.status_code}", flush=True)
                    continue

                audio_streams = stream_res.json().get("audioStreams", [])
                if not audio_streams:
                    continue

                audio_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                direct_stream_url = audio_streams[0]["url"]

            elif api_type == "invidious":
                search_url = f"{api_base}/api/v1/search?q={requests.utils.quote(search_query)}&type=video"
                res = requests.get(search_url, headers=headers, timeout=6)
                
                if res.status_code != 200:
                    print(f"[{api_base}] Search HTTP status: {res.status_code}", flush=True)
                    continue

                items = res.json()
                if not isinstance(items, list) or not items:
                    continue

                video_id = items[0].get("videoId")
                if not video_id:
                    continue

                print(f"Found Video ID '{video_id}' on {api_base}", flush=True)

                stream_res = requests.get(f"{api_base}/api/v1/videos/{video_id}", headers=headers, timeout=8)
                if stream_res.status_code != 200:
                    print(f"[{api_base}] Video endpoint returned HTTP {stream_res.status_code}", flush=True)
                    continue

                adaptive_formats = stream_res.json().get("adaptiveFormats", [])
                audio_streams = [f for f in adaptive_formats if f.get("type", "").startswith("audio/")]
                if not audio_streams:
                    continue

                audio_streams.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
                direct_stream_url = audio_streams[0]["url"]

            if not direct_stream_url:
                continue

            print(f"Downloading stream directly to MP3 from {api_base}...", flush=True)

            # 1. Primary method: Convert stream to clean MP3 using ffmpeg
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-headers", "User-Agent: Mozilla/5.0\r\n",
                "-i", direct_stream_url,
                "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                output_mp3_path
            ]

            ffmpeg_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)

            if ffmpeg_res.returncode == 0 and os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 100000:
                print(f"--- [SUCCESS] Downloaded audio file via {api_base}! ---", flush=True)
                return True

            # 2. Fallback method: Direct HTTP stream write
            with requests.get(direct_stream_url, headers=headers, stream=True, timeout=30) as stream_response:
                if stream_response.status_code == 200:
                    with open(output_mp3_path, "wb") as f:
                        for chunk in stream_response.iter_content(chunk_size=8192):
                            f.write(chunk)

                    if os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 100000:
                        print(f"--- [SUCCESS] Stream saved directly via {api_base}! ---", flush=True)
                        return True

        except Exception as e:
            print(f"Proxy engine error on {api_base}: {e}", flush=True)
            continue

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
        clean_search_term = f"{artist_name} {song_title}" if artist_name != "Unknown Artist" else search_query

        # 3. Download via Proxy Engine
        temp_mp3 = os.path.join(temp_dir, "downloaded_track.mp3")
        download_success = download_via_proxy_api(clean_search_term, temp_mp3)

        if not download_success or not os.path.exists(temp_mp3):
            print("--- [ERROR] All proxy instances failed ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download failed")
            return

        # 4. Write clean ID3 tags directly to MP3
        try:
            try:
                tags = ID3(temp_mp3)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=song_title))
            tags.add(TPE1(encoding=3, text=artist_name))
            tags.save(temp_mp3)
            print("Successfully embedded clean ID3 tags into MP3!", flush=True)
        except Exception as e:
            print(f"ID3 write notice: {e}", flush=True)

        # 5. Move finished MP3 to primary cache directory
        shutil.move(temp_mp3, audio_path)
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
