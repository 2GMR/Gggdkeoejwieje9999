import os
import re
import json
import time
import shutil
import tempfile
import subprocess
import logging
import base64
import hmac
import hashlib
import threading
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Auto-detect serverless environments where we cannot download/transcode
# (timeout 10s, no ffmpeg, ephemeral filesystem). On Vercel/Netlify/etc. we
# fall back to "URL relay" mode: ask TikWM for a no-watermark URL and hand
# it to Telegram, who fetches it directly. Quality is 540p H.264.
SERVERLESS_MODE = bool(
    os.getenv("VERCEL")
    or os.getenv("NETLIFY")
    or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    or os.getenv("SERVERLESS_MODE")
)

YDL_API_HOSTNAMES = [
    "api16-normal-c-useast1a.tiktokv.com",
    "api22-normal-c-useast1a.tiktokv.com",
    "api22-normal-c-alisg.tiktokv.com",
    "api16-normal-c-alisg.tiktokv.com",
]

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
    "format": "best",
    "socket_timeout": 10,
    "retries": 1,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G960F) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        )
    },
}


def _ydl_opts(hostname: str) -> dict:
    opts = dict(YDL_BASE_OPTS)
    opts["extractor_args"] = {
        "tiktok": {
            "api_hostname": [hostname],
            "app_name": ["musical_ly"],
            "app_version": ["34.1.2"],
            "manifest_app_version": ["2023401020"],
        }
    }
    return opts

TIKWM_ENDPOINTS = [
    "https://www.tikwm.com/api/",
    "https://tikwm.com/api/",
]
TIKWM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; SM-G960F) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tikwm.com/",
}

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Maximum video size to upload via Telegram bot API (50 MB hard limit)
MAX_UPLOAD_BYTES = 49 * 1024 * 1024

# ffmpeg used to transcode HEVC → H.264 for smooth playback on all Telegram
# clients. Disabled if ffmpeg is missing (the bot still sends the original).
FFMPEG_BIN = shutil.which("ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")

WELCOME = (
    "👋 أهلاً بك!\n\n"
    "أرسل لي رابط فيديو أو صور من تيك توك وسأقوم بتحميله لك بأعلى جودة.\n\n"
    "🎬 يدعم: الفيديوهات والصور (Slideshow)"
)

HELP = (
    "📖 طريقة الاستخدام:\n\n"
    "1️⃣ انسخ رابط الفيديو/الصور من تيك توك\n"
    "2️⃣ أرسله لي هنا\n"
    "3️⃣ سأقوم بإرساله لك مباشرة بأعلى جودة\n\n"
    "🚀 لا توجد علامة مائية، وتحميل سريع جداً."
)


# ---------------------------------------------------------------------------
# Telegram file_id cache
# ---------------------------------------------------------------------------
# Once Telegram has fetched a video, it stores it on its CDN and gives us a
# `file_id`. Future requests for the same TikTok video can be served by
# sending that file_id back — Telegram delivers the cached file directly,
# zero bytes through our server. This is the single biggest bandwidth win
# for viral content (e.g. trending videos requested by hundreds of users).
_FILE_ID_CACHE: dict[str, str] = {}
_FILE_ID_CACHE_LOCK = threading.Lock()
_FILE_ID_CACHE_PATH = os.getenv("FILE_ID_CACHE_PATH", "/tmp/tiktok_file_id_cache.json")
_FILE_ID_CACHE_MAX = int(os.getenv("FILE_ID_CACHE_MAX", "20000"))


def _load_file_id_cache():
    global _FILE_ID_CACHE
    try:
        with open(_FILE_ID_CACHE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _FILE_ID_CACHE = data
            logger.info(f"file_id cache: loaded {len(_FILE_ID_CACHE)} entries")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"file_id cache: load failed: {e}")


def _save_file_id_cache():
    try:
        with _FILE_ID_CACHE_LOCK:
            snapshot = dict(_FILE_ID_CACHE)
        tmp_path = _FILE_ID_CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp_path, _FILE_ID_CACHE_PATH)
    except Exception as e:
        logger.warning(f"file_id cache: save failed: {e}")


def _extract_video_id(url: str) -> str | None:
    """Return the canonical TikTok video ID (e.g. '7234567890123456789')."""
    m = re.search(r"/video/(\d+)", url)
    if m:
        return m.group(1)
    if any(d in url.lower() for d in ("vm.tiktok.com", "vt.tiktok.com")):
        try:
            canonical = _resolve_canonical_url(url)
            m = re.search(r"/video/(\d+)", canonical or "")
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def _cache_get(video_id: str) -> str | None:
    if not video_id:
        return None
    with _FILE_ID_CACHE_LOCK:
        return _FILE_ID_CACHE.get(video_id)


def _cache_set(video_id: str, file_id: str):
    if not video_id or not file_id:
        return
    with _FILE_ID_CACHE_LOCK:
        if len(_FILE_ID_CACHE) >= _FILE_ID_CACHE_MAX:
            try:
                first = next(iter(_FILE_ID_CACHE))
                _FILE_ID_CACHE.pop(first, None)
            except StopIteration:
                pass
        _FILE_ID_CACHE[video_id] = file_id
    _save_file_id_cache()


def _cache_drop(video_id: str):
    if not video_id:
        return
    with _FILE_ID_CACHE_LOCK:
        _FILE_ID_CACHE.pop(video_id, None)
    _save_file_id_cache()


def tg(method: str, payload: dict, timeout: int = 10):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        logger.error(f"Telegram API error ({method}): {e}")
        return None


def send_message(chat_id: int, text: str, reply_to: int = None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg("sendMessage", payload)


def send_chat_action(chat_id: int, action: str = "upload_video"):
    return tg("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)


def is_tiktok_url(text: str) -> bool:
    if not text:
        return False
    text = text.lower().strip()
    return any(d in text for d in [
        "tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "m.tiktok.com",
    ])


def _resolve_canonical_url(url: str) -> str:
    """Follow short-link redirects (vt.tiktok.com / vm.tiktok.com) to the
    canonical https://www.tiktok.com/@user/video/<id> URL."""
    try:
        r = requests.head(url, headers=WEB_HEADERS, allow_redirects=True, timeout=10)
        return r.url or url
    except Exception:
        return url


def _walk_for_video(node, depth: int = 0):
    """Recursively walk a JSON tree to find the TikTok video node containing
    bitrateInfo (the list of available qualities)."""
    if depth > 10:
        return None
    if isinstance(node, dict):
        if (
            "video" in node
            and isinstance(node["video"], dict)
            and ("bitrateInfo" in node["video"] or "playAddr" in node["video"])
        ):
            return node
        for v in node.values():
            r = _walk_for_video(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = _walk_for_video(item, depth + 1)
            if r:
                return r
    return None


def _walk_for_images(node, depth: int = 0):
    """Find a slideshow / image-post node in the rehydration JSON."""
    if depth > 10:
        return None
    if isinstance(node, dict):
        if "imagePost" in node and isinstance(node["imagePost"], dict):
            return node["imagePost"]
        for v in node.values():
            r = _walk_for_images(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = _walk_for_images(item, depth + 1)
            if r:
                return r
    return None


def _extract_via_web(url: str) -> dict:
    """Primary extractor: scrape TikTok's web page directly.

    The page embeds a __UNIVERSAL_DATA_FOR_REHYDRATION__ <script> tag with the
    full bitrateInfo list (5 qualities including 720p HEVC). This works from
    cloud IPs that the internal API endpoints block.
    """
    canonical = _resolve_canonical_url(url)
    r = requests.get(canonical, headers=WEB_HEADERS, allow_redirects=True, timeout=15)
    r.raise_for_status()
    html = r.text

    m = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return {"type": "none"}

    try:
        data = json.loads(m.group(1))
    except Exception as e:
        logger.warning(f"web extractor: failed to parse JSON: {e}")
        return {"type": "none"}

    # Try slideshow first
    img_post = _walk_for_images(data)
    if img_post:
        images = []
        for img in img_post.get("images", []):
            url_list = (img.get("imageURL") or {}).get("urlList") or []
            if url_list:
                images.append(url_list[0])
        title = ""
        # Caption usually lives a level up in itemStruct
        if images:
            return {"type": "images", "urls": images, "title": title}

    # Video
    vnode = _walk_for_video(data)
    if not vnode:
        return {"type": "none"}

    v = vnode["video"]
    title = vnode.get("desc") or ""

    # Pick best quality from bitrateInfo. Prefer the highest resolution
    # (GearName starting with "adapt_lower_720" or any 720+ height); among
    # equal heights prefer higher bitrate.
    bitrate_info = v.get("bitrateInfo") or []

    def _height(b):
        # GearName usually contains the resolution e.g. "adapt_lower_720_1"
        gn = (b.get("GearName") or "").lower()
        m = re.search(r"(\d{3,4})", gn)
        return int(m.group(1)) if m else 0

    candidates = []
    for b in bitrate_info:
        play_addr = b.get("PlayAddr") or {}
        urls = play_addr.get("UrlList") or []
        if not urls:
            continue
        candidates.append({
            "height": _height(b),
            "bitrate": b.get("Bitrate") or 0,
            "size": int(play_addr.get("DataSize") or 0),
            "codec": b.get("CodecType") or "",
            "url": urls[0],
            "backup_urls": urls,
        })

    chosen = None
    if candidates:
        candidates.sort(key=lambda c: (c["height"], c["bitrate"]), reverse=True)
        chosen = candidates[0]

    video_url = chosen["url"] if chosen else (v.get("playAddr") or v.get("downloadAddr"))
    if not video_url:
        return {"type": "none"}

    # Width/height: bitrateInfo gear gives us actual resolution, video.{width,height}
    # is only the smallest variant. Compute proper width based on aspect ratio.
    src_w = int(v.get("width") or 0)
    src_h = int(v.get("height") or 0)
    if chosen and chosen["height"] and src_w and src_h:
        out_h = chosen["height"]
        out_w = int(round(src_w * out_h / src_h))
    else:
        out_h = src_h
        out_w = src_w

    return {
        "type": "video",
        "url": video_url,
        "backup_urls": chosen["backup_urls"] if chosen else [video_url],
        "title": title,
        "duration": int(v.get("duration") or 0),
        "width": out_w,
        "height": out_h,
        "size": chosen["size"] if chosen else 0,
        "thumbnail": (v.get("cover") or v.get("originCover") or ""),
        "needs_referer": True,
    }


def _tikwm_call(url: str) -> dict | None:
    """Single call to TikWM. Returns parsed media dict or None on failure."""
    for endpoint in TIKWM_ENDPOINTS:
        try:
            r = requests.post(
                endpoint,
                data={"url": url, "hd": "1"},
                headers=TIKWM_HEADERS,
                timeout=12,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                msg = (data.get("msg") or "").lower()
                if "limit" in msg or "rate" in msg or "frequen" in msg:
                    return {"_rate_limited": True}
                continue

            d = data["data"]

            images = d.get("images") or []
            if images:
                return {
                    "type": "images",
                    "urls": list(images),
                    "title": d.get("title") or "",
                }

            play_url = d.get("play")
            hd_url = d.get("hdplay")
            wm_url = d.get("wmplay")

            # Always prefer hdplay when present — it returns 720p H.264 with
            # no watermark, and the URL works without a Referer header so
            # Telegram can fetch it directly (zero bandwidth on our server).
            video_url = hd_url or play_url or wm_url

            if video_url:
                return {
                    "type": "video",
                    "url": video_url,
                    "title": d.get("title") or "",
                    "duration": int(d.get("duration") or 0),
                    "width": 0,
                    "height": 0,
                    "thumbnail": d.get("cover") or d.get("origin_cover"),
                }
        except Exception:
            continue
    return None


def _extract_via_tikwm(url: str) -> dict:
    """Fallback extractor: TikWM API. Returns no-watermark URLs.
    Retries up to 2 times on rate-limit (free tier allows 1 req/sec).
    """
    for attempt in range(3):
        result = _tikwm_call(url)
        if result is None:
            raise RuntimeError("tikwm: no result")
        if result.get("_rate_limited"):
            time.sleep(1.5)
            continue
        if result.get("type") in ("video", "images"):
            return result
        raise RuntimeError("tikwm: unknown response")
    raise RuntimeError("tikwm: rate-limited after retries")


def _is_h264(vcodec: str) -> bool:
    if not vcodec:
        return False
    v = vcodec.lower()
    return "avc" in v or "h264" in v or v.startswith("h.264")


def _pick_best_format(formats: list) -> dict | None:
    """Pick the best video format from yt-dlp.

    Strategy: prefer the highest resolution. If two formats share a resolution,
    prefer H.264 (universally compatible with Telegram's preview player) over
    HEVC. Within the same codec, prefer higher bitrate.
    """
    candidates = [
        f for f in formats
        if f.get("url") and f.get("vcodec") and f.get("vcodec") != "none"
    ]
    if not candidates:
        return None

    def score(f):
        return (
            f.get("height") or 0,
            1 if _is_h264(f.get("vcodec") or "") else 0,
            f.get("tbr") or 0,
        )

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _extract_via_ytdlp(url: str) -> dict:
    """Primary extractor: yt-dlp picks the highest resolution available.

    For TikTok videos this is usually 720p HEVC, which Telegram modern clients
    handle well when we pass width/height/duration metadata explicitly.
    Tries multiple TikTok API hostnames since some get rate-limited per IP.
    """
    info = None
    last_err = None
    for hostname in YDL_API_HOSTNAMES:
        try:
            with YoutubeDL(_ydl_opts(hostname)) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and (info.get("formats") or info.get("url") or info.get("entries")):
                break
        except Exception as e:
            last_err = e
            info = None
            continue

    if not info:
        if last_err:
            raise last_err
        return {"type": "none"}

    # Slideshow / images
    if info.get("_type") == "playlist" or info.get("entries"):
        entries = info.get("entries") or []
        images = []
        for e in entries:
            if not e:
                continue
            img_url = e.get("url") or (e.get("thumbnails") or [{}])[-1].get("url")
            if img_url:
                images.append(img_url)
        if images:
            return {
                "type": "images",
                "urls": images,
                "title": info.get("title") or "",
            }

    if info.get("vcodec") == "none" or (not info.get("url") and info.get("thumbnails")):
        thumbs = info.get("thumbnails") or []
        images = [t.get("url") for t in thumbs if t.get("url")]
        if images:
            return {
                "type": "images",
                "urls": list(dict.fromkeys(images)),
                "title": info.get("title") or "",
            }

    best = _pick_best_format(info.get("formats") or [])
    if best:
        return {
            "type": "video",
            "url": best.get("url"),
            "title": info.get("title") or "",
            "duration": int(info.get("duration") or 0),
            "width": int(best.get("width") or info.get("width") or 0),
            "height": int(best.get("height") or info.get("height") or 0),
            "thumbnail": info.get("thumbnail"),
        }

    if info.get("url"):
        return {
            "type": "video",
            "url": info.get("url"),
            "title": info.get("title") or "",
            "duration": int(info.get("duration") or 0),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "thumbnail": info.get("thumbnail"),
        }

    return {"type": "none"}


def extract_media(url: str) -> dict:
    """Extract direct media URLs without downloading.

    On serverless (Vercel/Lambda) we go straight to TikWM because we cannot
    download/transcode anyway — its `play` URL is fetched by Telegram itself
    and works without a Referer header. Saves precious milliseconds against
    the 10s timeout.

    On a full server we try in this order:
      1) Web scraper — full bitrateInfo (720p HEVC, highest quality).
      2) yt-dlp — fallback if the web page format changes.
      3) TikWM API — last-resort fallback (540p H.264 only).
    """
    if SERVERLESS_MODE:
        # 1) TikWM HD endpoint (`hdplay` URL) → 720p H.264, no Referer needed,
        #    Telegram fetches it directly = ZERO bandwidth on our server.
        #    This is the path we want for ~95% of requests.
        try:
            result = _extract_via_tikwm(url)
            if result and result.get("type") in ("video", "images"):
                return result
        except Exception as e:
            logger.warning(f"TikWM failed, trying web extractor: {e}")
        # 2) Web extractor → 720p HEVC native, but URL is Referer-locked so
        #    we'll have to proxy it through Render (consumes bandwidth).
        #    Only used when TikWM is unreachable or rate-limited.
        try:
            return _extract_via_web(url)
        except Exception as e:
            logger.error(f"web extractor also failed: {e}")
            raise

    try:
        result = _extract_via_web(url)
        if result and result.get("type") in ("video", "images"):
            return result
    except Exception as e:
        logger.warning(f"web extractor failed, trying yt-dlp: {e}")

    try:
        result = _extract_via_ytdlp(url)
        if result and result.get("type") in ("video", "images"):
            return result
    except Exception as e:
        logger.warning(f"yt-dlp extractor failed, falling back to TikWM: {e}")

    try:
        return _extract_via_tikwm(url)
    except Exception as e:
        logger.error(f"TikWM extractor failed: {e}")
        raise


def _probe_video_codec(path: str) -> str | None:
    """Return the video codec name (e.g. 'h264', 'hevc') or None."""
    if not FFPROBE_BIN:
        return None
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=8,
        )
        return (r.stdout or "").strip().lower() or None
    except Exception as e:
        logger.warning(f"ffprobe failed: {e}")
        return None


def transcode_to_h264(src_path: str) -> str | None:
    """Transcode a video to H.264 + AAC with constant frame rate and faststart.

    This makes the file play smoothly on every Telegram client (HEVC / H.265
    causes stuttering on Telegram Desktop and many Android devices).
    Returns the new file path, or None on failure.
    """
    if not FFMPEG_BIN:
        return None

    dst_fd, dst_path = tempfile.mkstemp(prefix="tiktok_h264_", suffix=".mp4")
    os.close(dst_fd)
    try:
        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-i", src_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-r", "30",                       # constant 30fps fixes stutter
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart",
            dst_path,
        ]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        elapsed = time.time() - t0
        if r.returncode != 0:
            logger.warning(f"ffmpeg transcode failed (exit {r.returncode}): {r.stderr[:200]}")
            try: os.unlink(dst_path)
            except Exception: pass
            return None
        size = os.path.getsize(dst_path)
        if size < 10_000:
            try: os.unlink(dst_path)
            except Exception: pass
            return None
        if size > MAX_UPLOAD_BYTES:
            logger.info(f"transcoded file too large ({size} bytes)")
            try: os.unlink(dst_path)
            except Exception: pass
            return None
        logger.info(f"transcoded HEVC→H.264 in {elapsed:.1f}s, {size/1024:.0f} KB")
        return dst_path
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg transcode timed out")
        try: os.unlink(dst_path)
        except Exception: pass
        return None
    except Exception as e:
        logger.warning(f"ffmpeg transcode error: {e}")
        try: os.unlink(dst_path)
        except Exception: pass
        return None


def download_to_tempfile(media: dict) -> str | None:
    """Download the video to a local temp file with the proper Referer header.
    TikTok's CDN requires a tiktok.com referer — Telegram's URL fetcher does
    not send one, so passing the URL directly to sendVideo often fails or
    yields the lowest-quality fallback.

    Returns the temp file path, or None on failure.
    """
    urls = media.get("backup_urls") or [media.get("url")]
    headers = {
        "User-Agent": WEB_HEADERS["User-Agent"],
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
        "Range": "bytes=0-",
    }
    for u in urls:
        if not u:
            continue
        try:
            r = requests.get(u, headers=headers, stream=True, timeout=25)
            if r.status_code not in (200, 206):
                logger.warning(f"download status {r.status_code} for {u[:80]}")
                continue
            total = int(r.headers.get("Content-Length") or 0)
            if total and total > MAX_UPLOAD_BYTES:
                logger.info(f"video too large ({total} bytes), will fall back to URL")
                return None

            tmp = tempfile.NamedTemporaryFile(prefix="tiktok_", suffix=".mp4", delete=False)
            written = 0
            try:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        tmp.close()
                        os.unlink(tmp.name)
                        logger.info("video exceeded max size during stream")
                        return None
                    tmp.write(chunk)
                tmp.close()
                if written < 10_000:
                    os.unlink(tmp.name)
                    continue
                logger.info(f"downloaded {written/1024:.0f} KB to {tmp.name}")
                return tmp.name
            except Exception as e:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                logger.warning(f"download stream error: {e}")
                continue
        except Exception as e:
            logger.warning(f"download request failed: {e}")
            continue
    return None


def send_video_by_file(chat_id: int, media: dict, file_path: str, reply_to: int = None):
    """Upload the local file via multipart — Telegram won't re-fetch the URL,
    preserving the original quality (especially HEVC 720p)."""
    data = {
        "chat_id": str(chat_id),
        "supports_streaming": "true",
        "caption": "Tik : 1l.u",
    }
    if media.get("duration"):
        data["duration"] = str(int(media["duration"]))
    if media.get("width"):
        data["width"] = str(int(media["width"]))
    if media.get("height"):
        data["height"] = str(int(media["height"]))
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)

    files = {"video": ("video.mp4", open(file_path, "rb"), "video/mp4")}
    if media.get("thumbnail"):
        try:
            tr = requests.get(media["thumbnail"], timeout=8)
            if tr.status_code == 200 and tr.content:
                files["thumbnail"] = ("thumb.jpg", tr.content, "image/jpeg")
        except Exception:
            pass

    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendVideo",
            data=data,
            files=files,
            timeout=120,
        )
        return r.json()
    except Exception as e:
        logger.error(f"sendVideo upload failed: {e}")
        return None
    finally:
        try:
            files["video"][1].close()
        except Exception:
            pass


PROXY_SECRET = (
    os.getenv("WEBHOOK_SECRET")
    or os.getenv("SESSION_SECRET")
    or (BOT_TOKEN or "fallback-secret")
).encode()


def _sign_proxy_url(src_url: str) -> str:
    """Build a tamper-proof proxy URL pointing at /api/v/<token>.

    The token contains the source URL plus an HMAC signature so nobody can
    reuse our server to proxy arbitrary URLs.
    """
    raw = src_url.encode()
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(PROXY_SECRET, raw, hashlib.sha256).hexdigest()[:16]
    return f"{b64}.{sig}"


def _verify_proxy_token(token: str) -> str | None:
    try:
        b64, sig = token.split(".", 1)
        pad = "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64 + pad)
        expected = hmac.new(PROXY_SECRET, raw, hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        return raw.decode()
    except Exception:
        return None


def _build_proxy_url(src_url: str) -> str | None:
    """Return a public Render URL that proxies the TikTok CDN with Referer.
    Returns None if WEBHOOK_URL is not configured (proxy unavailable)."""
    base = (os.getenv("WEBHOOK_URL") or "").rstrip("/")
    if not base:
        return None
    return f"{base}/api/v/{_sign_proxy_url(src_url)}"


def send_video_by_url(chat_id: int, media: dict, reply_to: int = None):
    """Last-resort: hand the URL to Telegram. Quality may degrade because
    Telegram fetches without a proper Referer."""
    payload = {
        "chat_id": chat_id,
        "video": media["url"],
        "supports_streaming": True,
        "caption": "Tik : 1l.u",
    }
    if media.get("duration"):
        payload["duration"] = int(media["duration"])
    if media.get("width"):
        payload["width"] = int(media["width"])
    if media.get("height"):
        payload["height"] = int(media["height"])
    if media.get("thumbnail"):
        payload["thumb"] = media["thumbnail"]
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg("sendVideo", payload, timeout=30)


def send_images_as_group(chat_id: int, images: list, reply_to: int = None):
    """Send images as media group(s). Telegram limits to 10 per group."""
    results = []
    for i in range(0, len(images), 10):
        chunk = images[i:i + 10]
        media = []
        for idx, img in enumerate(chunk):
            item = {"type": "photo", "media": img}
            if i == 0 and idx == 0:
                item["caption"] = "Tik : 1l.u"
            media.append(item)
        payload = {"chat_id": chat_id, "media": media}
        if reply_to and i == 0:
            payload["reply_to_message_id"] = reply_to
        results.append(tg("sendMediaGroup", payload, timeout=20))
    return results


class ProgressIndicator:
    """Sends a placeholder message and animates a progress bar while the
    actual download/upload runs in the foreground. The message is deleted
    when stop_and_delete() is called, just before the video appears."""

    def __init__(self, chat_id: int, reply_to: int = None):
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.msg_id = None
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _render(pct: int) -> str:
        bars = 10
        filled = max(0, min(bars, round(pct / 100 * bars)))
        bar = "▰" * filled + "▱" * (bars - filled)
        return f"⏳ جاري التحميل\n{bar}  {pct}%"

    def start(self):
        payload = {
            "chat_id": self.chat_id,
            "text": self._render(15),
            "disable_web_page_preview": True,
        }
        if self.reply_to:
            payload["reply_to_message_id"] = self.reply_to
        res = tg("sendMessage", payload, timeout=5)
        self.msg_id = ((res or {}).get("result") or {}).get("message_id")
        if self.msg_id:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def _tick(self):
        # Animate fake progress while extraction/upload is in flight.
        # Telegram rate-limits editMessage to ~1/sec per chat — we stay well
        # under that.
        for pct in (35, 60, 85):
            if self._stop.wait(1.4):
                return
            try:
                tg("editMessageText", {
                    "chat_id": self.chat_id,
                    "message_id": self.msg_id,
                    "text": self._render(pct),
                }, timeout=5)
            except Exception:
                return

    def stop_and_delete(self):
        self._stop.set()
        if self._thread:
            try:
                self._thread.join(timeout=0.5)
            except Exception:
                pass
        if self.msg_id:
            try:
                tg("deleteMessage", {
                    "chat_id": self.chat_id,
                    "message_id": self.msg_id,
                }, timeout=5)
            except Exception:
                pass
            self.msg_id = None


def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    msg_id = message.get("message_id")
    text = (message.get("text") or message.get("caption") or "").strip()

    if not text:
        return

    # Commands
    if text.startswith("/start"):
        send_message(chat_id, WELCOME)
        return
    if text.startswith("/help"):
        send_message(chat_id, HELP)
        return

    if not is_tiktok_url(text):
        send_message(
            chat_id,
            "⚠️ الرجاء إرسال رابط صحيح من تيك توك.",
            reply_to=msg_id,
        )
        return

    # Extract first URL token from text
    url = next((w for w in text.split() if is_tiktok_url(w)), text)

    send_chat_action(chat_id, "upload_video")

    # ---- Telegram file_id cache hit: serve instantly with zero bandwidth ----
    video_id = _extract_video_id(url)
    if video_id:
        cached = _cache_get(video_id)
        if cached:
            logger.info(f"cache HIT video_id={video_id} → sending by file_id")
            payload = {
                "chat_id": chat_id,
                "video": cached,
                "supports_streaming": True,
                "caption": "Tik : 1l.u",
            }
            if msg_id:
                payload["reply_to_message_id"] = msg_id
            res = tg("sendVideo", payload, timeout=15)
            if res and res.get("ok"):
                return
            # Cached file_id may have expired or been deleted by Telegram.
            logger.warning(f"file_id send failed, dropping cache for {video_id}: {res}")
            _cache_drop(video_id)

    progress = ProgressIndicator(chat_id, reply_to=msg_id)
    progress.start()

    try:
        media = extract_media(url)
    except Exception as e:
        progress.stop_and_delete()
        logger.exception("extract_media failed")
        send_message(
            chat_id,
            f"❌ تعذّر استخراج المحتوى. تأكد من صحة الرابط.\n\nالسبب: {str(e)[:120]}",
            reply_to=msg_id,
        )
        return

    try:
        _dispatch_media(chat_id, msg_id, url, media, video_id, progress)
    finally:
        progress.stop_and_delete()


def _dispatch_media(chat_id, msg_id, url, media, video_id, progress):
    if media["type"] == "video":
        res = None

        if SERVERLESS_MODE:
            # Lightweight URL-relay mode. Telegram fetches the video itself.
            # If the source URL needs a Referer header (web extractor → 720p
            # HEVC native quality), wrap it in our /api/v/<token> proxy so
            # Telegram fetches through us with the proper header. The proxy
            # streams bytes without touching CPU/RAM.
            relay_media = media
            if media.get("needs_referer"):
                proxied = _build_proxy_url(media["url"])
                if proxied:
                    relay_media = dict(media)
                    relay_media["url"] = proxied
            res = send_video_by_url(chat_id, relay_media, reply_to=msg_id)
        else:
            # Full mode: download → optional HEVC→H.264 transcode → upload.
            tmp_path = None
            transcoded_path = None
            try:
                tmp_path = download_to_tempfile(media)
                upload_path = tmp_path
                if tmp_path:
                    # If the source is HEVC, transcode to H.264 for smooth
                    # playback on every Telegram client.
                    codec = _probe_video_codec(tmp_path)
                    if codec in ("hevc", "h265"):
                        send_chat_action(chat_id, "upload_video")
                        transcoded_path = transcode_to_h264(tmp_path)
                        if transcoded_path:
                            upload_path = transcoded_path

                    res = send_video_by_file(chat_id, media, upload_path, reply_to=msg_id)
            finally:
                for p in (tmp_path, transcoded_path):
                    if p:
                        try: os.unlink(p)
                        except Exception: pass

            if not res or not res.get("ok"):
                logger.warning(f"file upload failed, trying URL: {res}")
                res = send_video_by_url(chat_id, media, reply_to=msg_id)

        if not res or not res.get("ok"):
            send_message(
                chat_id,
                "⚠️ تعذّر إرسال الفيديو مباشرة. هذا هو الرابط المباشر:\n\n" + media["url"],
                reply_to=msg_id,
            )
        else:
            # Capture Telegram's file_id so future requests for the same
            # video are served from Telegram's CDN with zero bandwidth.
            try:
                file_id = (res.get("result") or {}).get("video", {}).get("file_id")
                if file_id and video_id:
                    _cache_set(video_id, file_id)
                    logger.info(f"cached file_id for {video_id}")
            except Exception:
                pass
    elif media["type"] == "images":
        send_chat_action(chat_id, "upload_photo")
        send_images_as_group(chat_id, media["urls"], reply_to=msg_id)
    else:
        send_message(
            chat_id,
            "❌ لم أتمكن من العثور على محتوى قابل للتحميل في هذا الرابط.",
            reply_to=msg_id,
        )


@app.route("/api/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "service": "tiktok-bot"}), 200

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured")
        return jsonify({"ok": False, "error": "BOT_TOKEN missing"}), 500

    # Optional secret-token verification (Telegram sends header X-Telegram-Bot-Api-Secret-Token)
    secret = os.getenv("WEBHOOK_SECRET")
    if secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != secret:
            logger.warning("Invalid webhook secret token")
            return jsonify({"ok": False}), 401

    try:
        update = request.get_json(force=True, silent=True) or {}
    except Exception:
        update = {}

    try:
        handle_update(update)
    except Exception:
        logger.exception("handle_update failed")

    # Always return 200 quickly so Telegram does not retry/queue
    return jsonify({"ok": True}), 200


@app.route("/api/setwebhook", methods=["GET"])
def set_webhook():
    """One-time helper: visit this URL to register the webhook with Telegram."""
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "BOT_TOKEN missing"}), 500
    base = os.getenv("WEBHOOK_URL")
    if not base:
        return jsonify({"ok": False, "error": "WEBHOOK_URL missing"}), 500
    url = base.rstrip("/") + "/api/webhook"
    payload = {
        "url": url,
        "max_connections": 100,
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": True,
    }
    secret = os.getenv("WEBHOOK_SECRET")
    if secret:
        payload["secret_token"] = secret
    res = tg("setWebhook", payload, timeout=10)
    return jsonify(res or {"ok": False}), 200


@app.route("/api/v/<token>", methods=["GET", "HEAD"])
def proxy_video(token):
    """Stream a TikTok CDN video through Render with the proper Referer.

    Telegram's URL fetcher does not send a Referer header, so TikTok's CDN
    blocks it (or returns the lowest quality). We sit in the middle: we
    forward Telegram's request to TikTok adding the required header, and
    stream the bytes back. CPU cost is minimal (just copying bytes), but
    bandwidth is consumed once per fetched video.
    """
    src = _verify_proxy_token(token)
    if not src:
        return jsonify({"ok": False, "error": "invalid token"}), 403

    headers = {
        "User-Agent": WEB_HEADERS["User-Agent"],
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
    }
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng

    try:
        upstream = requests.get(src, headers=headers, stream=True, timeout=20)
    except Exception as e:
        logger.warning(f"proxy upstream error: {e}")
        return jsonify({"ok": False, "error": "upstream"}), 502

    if upstream.status_code not in (200, 206):
        upstream.close()
        return jsonify({"ok": False, "error": f"upstream {upstream.status_code}"}), 502

    response_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Cache-Control": "public, max-age=3600",
    }
    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if h in upstream.headers:
            response_headers[h] = upstream.headers[h]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=response_headers,
    )


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "service": "tiktok-bot",
        "cached_videos": len(_FILE_ID_CACHE),
    }), 200


# Initialise the file_id cache on import (after the file path is known).
_load_file_id_cache()
