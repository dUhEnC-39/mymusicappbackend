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
    Resolves YouTube Video ID directly by scraping YouTube search HTML.
    """
    print(f"--- [RESOLVER] Resolving YouTube Video ID for '{search_query}' ---", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    try:
        yt_search_url = f"https://www.youtube.com/results?search_query={requests.utils.quote(search_query)}"
        res = requests.get(yt_search_url, headers=headers, timeout=5)
        if res.status_code == 200:
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', res.text)
            if matches:
                valid_ids = [m for m in matches if len(m) == 11]
                if valid_ids:
                    print(f"--- [RESOLVER SUCCESS] Resolved Video ID '{valid_ids[0]}' ---", flush=True)
                    return valid_ids[0]
    except Exception as e:
        print(f"YouTube HTML search notice: {e}", flush=True)

    # Fallback to yt-dlp flat playlist search
    try:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            f"ytsearch1:{search_query}",
            "--flat-playlist",
            "--print", "id",
            "--no-warnings"
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        v_id = res.stdout.strip()
        if res.returncode == 0 and len(v_id) == 11 and re.match(r'^[a-zA-Z0-9_-]{11}$', v_id):
            print(f"--- [RESOLVER SUCCESS] Resolved Video ID '{v_id}' via flat search ---", flush=True)
            return v_id
    except Exception as e:
        print(f"yt-dlp flat search notice: {e}", flush=True)

    return None

def download_via_cobalt(youtube_url: str, output_mp3_path: str):
    """Downloads audio stream using Cobalt API with browser origin headers."""
    print(f"--- [COBALT ENGINE] Processing '{youtube_url}' ---", flush=True)
    
    cobalt_instances = [
        "https://api.cobalt.tools/",
        "https://cobalt-api.kwippy.com/"
    ]
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://cobalt.tools",
        "Referer": "https://cobalt.tools/"
    }
    
    payload = {
        "url": youtube_url,
        "downloadMode": "audio",
        "audioFormat": "mp3"
    }

    for api_url in cobalt_instances:
        try:
            print(f"Querying Cobalt node: {api_url}...", flush=True)
            res = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                status = data.get("status")
                direct_link = None

                if status in ["tunnel", "redirect", "stream"]:
                    direct_link = data.get("url")
                elif status == "picker":
                    picker_items = data.get("picker", [])
                    if picker_items and isinstance(picker_items, list):
                        direct_link = picker_items[0].get("url")
                else:
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
            else:
                print(f"Cobalt node {api_url} returned HTTP {res.status_code}", flush=True)
        except Exception as e:
            print(f"Cobalt notice on {api_url}: {e}", flush=True)

    return False

def download_via_proxy_stream(video_id: str, output_mp3_path: str):
    """Fallback: Transcodes audio stream directly from active proxy instances."""
    print(f"--- [PROXY STREAM ENGINE] Fetching stream for Video ID '{video_id}' ---", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    nodes = [
        "https://inv.nadeko.net",
        "https://invidious.no-kyc.net",
        "https://invidious.nerdvpn.de",
        "https://api.piped.private.coffee"
    ]
    
    for base in nodes:
        try:
            print(f"Testing stream node: {base}...", flush=True)
            stream_url = None
            if "piped" in base:
                res = requests.get(f"{base}/streams/{video_id}", headers=headers, timeout=3)
                if res.status_code == 200:
                    audio_streams = res.json().get("audioStreams", [])
                    if audio_streams:
                        audio_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                        stream_url = audio_streams[0]["url"]
            else:
                res = requests.get(f"{base}/api/v1/videos/{video_id}", headers=headers, timeout=3)
                if res.status_code == 200:
                    formats = res.json().get("adaptiveFormats", [])
                    audio_streams = [f for f in formats if f.get("type", "").startswith("audio/")]
                    if audio_streams:
                        audio_streams.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
                        stream_url = audio_streams[0]["url"]

            if stream_url:
                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-headers", "User-Agent: Mozilla/5.0\r\n",
                    "-i", stream_url,
                    "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    output_mp3_path
                ]
                res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
                if res.returncode == 0 and os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 100000:
                    print(f"--- [SUCCESS] Stream converted to MP3 via {base}! ---", flush=True)
                    return True
        except Exception as e:
            print(f"Proxy stream notice on {base}: {e}", flush=True)

    return False

def download_via_soundcloud(search_query: str, temp_dir: str):
    """Fallback: Downloads audio from SoundCloud deep search."""
    print(f"--- [SOUNDCLOUD ENGINE] Searching SoundCloud for '{search_query}' ---", flush=True)
    output_template = os.path.join(temp_dir, "downloaded_track.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        f"scsearch10:{search_query}",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", output_template,
        "--no-playlist",
        "--match-filter", "!drm & duration > 30"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
        mp3_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if res.returncode == 0 and mp3_files:
            print("--- [SUCCESS] Downloaded audio via SoundCloud! ---", flush=True)
            return True
    except Exception as e:
        print(f"SoundCloud notice: {e}", flush=True)

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

        clean_search_term = f"{artist_name} {song_title}" if artist_name != "Unknown Artist" else search_query
        temp_mp3 = os.path.join(temp_dir, "downloaded_track.mp3")
        download_success = False

        # 2. Resolve YouTube Video ID
        video_id = resolve_youtube_video_id(clean_search_term)

        if video_id:
            youtube_url = f"https://www.youtube.com/watch?v={video_id}"

            # Engine 1: Cobalt API (with browser origin headers)
            download_success = download_via_cobalt(youtube_url, temp_mp3)

            # Engine 2: Direct Proxy Stream Transcode
            if not download_success or not os.path.exists(temp_mp3):
                print("Cobalt engine missed. Switching to Proxy Stream Engine...", flush=True)
                download_success = download_via_proxy_stream(video_id, temp_mp3)

        # Engine 3: SoundCloud Deep Search Fallback
        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]
        if not download_success or not downloaded_files:
            print("YouTube engines missed. Switching to SoundCloud Engine...", flush=True)
            download_success = download_via_soundcloud(clean_search_term, temp_dir)
            downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith(".mp3")]

        if not download_success or not downloaded_files:
            print("--- [ERROR] All engines failed to produce MP3 ---", flush=True)
            with open(failed_marker, "w") as f:
                f.write("Download failed")
            return

        downloaded_mp3_path = os.path.join(temp_dir, downloaded_files[0])

        # 3. Write clean ID3 tags directly to MP3
        try:
            tags = ID3(downloaded_mp3_path)
        except ID3NoHeaderError:
            tags = ID3()
        except Exception:
            tags = None

        if tags is not None:
            try:
                tags.add(TIT2(encoding=3, text=song_title))
                tags.add(TPE1(encoding=3, text=artist_name))
                tags.save(downloaded_mp3_path)
                print("Successfully embedded clean ID3 tags into MP3!", flush=True)
            except Exception as e:
                print(f"ID3 write notice: {e}", flush=True)

        # 4. Move finished MP3 to primary cache directory
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
