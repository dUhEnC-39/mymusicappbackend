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

def resolve_youtube_video_id(search_query: str):
    """
    Resolves YouTube Video ID via public Invidious REST APIs without scraping HTML.
    """
    print(f"--- [RESOLVER] Querying Invidious APIs for '{search_query}' ---", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    
    invidious_instances = [
        "https://inv.tux.pizza",
        "https://invidious.nerdvpn.de",
        "https://invidious.drgns.space",
        "https://inv.nadeko.net"
    ]

    for api_base in invidious_instances:
        try:
            search_url = f"{api_base}/api/v1/search?q={requests.utils.quote(search_query)}&type=video"
            res = requests.get(search_url, headers=headers, timeout=5)
            if res.status_code == 200:
                items = res.json()
                if isinstance(items, list) and len(items) > 0:
                    video_id = items[0].get("videoId")
                    if video_id:
                        print(f"--- [RESOLVER SUCCESS] Resolved Video ID '{video_id}' via {api_base} ---", flush=True)
                        return video_id, api_base
        except Exception as e:
            print(f"Resolver notice on {api_base}: {e}", flush=True)

    return None, None

def download_via_cobalt(youtube_url: str, output_mp3_path: str):
    """Downloads audio stream via Cobalt API."""
    print(f"--- [COBALT ENGINE] Processing '{youtube_url}' ---", flush=True)
    cobalt_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {
        "url": youtube_url,
        "downloadMode": "audio",
        "audioFormat": "mp3"
    }

    try:
        res = requests.post("https://api.cobalt.tools/", json=payload, headers=cobalt_headers, timeout=12)
        if res.status_code == 200:
            data = res.json()
            direct_link = data.get("url")
            if direct_link:
                print("Cobalt returned stream URL. Downloading MP3...", flush=True)
                with requests.get(direct_link, stream=True, timeout=30) as audio_res:
                    if audio_res.status_code == 200:
                        with open(output_mp3_path, "wb") as f:
                            for chunk in audio_res.iter_content(8192):
                                f.write(chunk)
                        
                        if os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 100000:
                            print("--- [SUCCESS] Downloaded audio via Cobalt API! ---", flush=True)
                            return True
    except Exception as e:
        print(f"Cobalt notice: {e}", flush=True)

    return False

def download_via_invidious_stream(api_base: str, video_id: str, output_mp3_path: str):
    """Fallback Engine: Downloads adaptive audio stream from Invidious and converts via ffmpeg."""
    print(f"--- [FALLBACK ENGINE] Downloading stream directly from {api_base} ---", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        stream_res = requests.get(f"{api_base}/api/v1/videos/{video_id}", headers=headers, timeout=8)
        if stream_res.status_code == 200:
            adaptive_formats = stream_res.json().get("adaptiveFormats", [])
            audio_streams = [f for f in adaptive_formats if f.get("type", "").startswith("audio/")]
            if audio_streams:
                audio_streams.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
                direct_stream_url = audio_streams[0]["url"]

                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-headers", "User-Agent: Mozilla/5.0\r\n",
                    "-i", direct_stream_url,
                    "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    output_mp3_path
                ]

                res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
                if res.returncode == 0 and os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 100000:
                    print(f"--- [SUCCESS] Stream converted to MP3 via {api_base}! ---", flush=True)
                    return True
    except Exception as e:
        print(f"Invidious stream notice: {e}", flush=True)

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

        # 2. Build search query & resolve YouTube video ID
        clean_search_term = f"{artist_name} {song_title}" if artist_name != "Unknown Artist" else search_query
        video_id, working_node = resolve_youtube_video_id(clean_search_term)

        if not video_id:
            print("--- [ERROR] Could not resolve YouTube Video ID ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Failed to resolve video ID")
            return

        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        temp_mp3 = os.path.join(temp_dir, "downloaded_track.mp3")

        # 3. Try Primary Engine (Cobalt API)
        download_success = download_via_cobalt(youtube_url, temp_mp3)

        # 4. Try Fallback Engine (Invidious Direct Stream) if Cobalt missed
        if not download_success or not os.path.exists(temp_mp3):
            print("Cobalt missed. Switching to Fallback Direct Stream Engine...", flush=True)
            download_success = download_via_invidious_stream(working_node, video_id, temp_mp3)

        if not download_success or not os.path.exists(temp_mp3):
            print("--- [ERROR] All engines failed to produce MP3 ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download failed")
            return

        # 5. Write clean ID3 tags directly to MP3
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

        # 6. Move finished MP3 to primary cache directory
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
