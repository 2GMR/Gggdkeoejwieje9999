import os
import re
import json
import time
import tempfile
import logging
import requests
from flask import Flask, request, jsonify
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

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
            play_size = d.get("size") or 0
            hd_size = d.get("hd_size") or 0

            video_url = None
            if hd_url and hd_size and play_size and hd_size > play_size * 1.15:
                video_url = hd_url
            elif play_url:
                video_url = play_url
            elif hd_url:
                video_url = hd_url
            elif wm_url:
                video_url = wm_url

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

    Strategy:
      1) Web scraper — reads __UNIVERSAL_DATA_FOR_REHYDRATION__ from the
         tiktok.com page. Gives access to the FULL bitrateInfo list
         including 720p HEVC (highest quality TikTok serves). Works from
         cloud IPs.
      2) yt-dlp — fallback if the web page format changes.
      3) TikWM API — last-resort fallback (returns 540p H.264 only).
    """
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
        "caption": "✅ تم التحميل\n\n@" + (os.getenv("BOT_USERNAME", "")),
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


def send_video_by_url(chat_id: int, media: dict, reply_to: int = None):
    """Last-resort: hand the URL to Telegram. Quality may degrade because
    Telegram fetches without a proper Referer."""
    payload = {
        "chat_id": chat_id,
        "video": media["url"],
        "supports_streaming": True,
        "caption": "✅ تم التحميل\n\n@" + (os.getenv("BOT_USERNAME", "")),
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
                item["caption"] = "✅ تم التحميل"
            media.append(item)
        payload = {"chat_id": chat_id, "media": media}
        if reply_to and i == 0:
            payload["reply_to_message_id"] = reply_to
        results.append(tg("sendMediaGroup", payload, timeout=20))
    return results


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

    try:
        media = extract_media(url)
    except Exception as e:
        logger.exception("extract_media failed")
        send_message(
            chat_id,
            f"❌ تعذّر استخراج المحتوى. تأكد من صحة الرابط.\n\nالسبب: {str(e)[:120]}",
            reply_to=msg_id,
        )
        return

    if media["type"] == "video":
        res = None
        tmp_path = None
        try:
            tmp_path = download_to_tempfile(media)
            if tmp_path:
                res = send_video_by_file(chat_id, media, tmp_path, reply_to=msg_id)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        # Fallback: try sending the URL directly
        if not res or not res.get("ok"):
            logger.warning(f"file upload failed, trying URL: {res}")
            res = send_video_by_url(chat_id, media, reply_to=msg_id)

        if not res or not res.get("ok"):
            send_message(
                chat_id,
                "⚠️ تعذّر إرسال الفيديو مباشرة. هذا هو الرابط المباشر:\n\n" + media["url"],
                reply_to=msg_id,
            )
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


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "tiktok-bot"}), 200
