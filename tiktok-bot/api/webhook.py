import os
import json
import time
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
      1) yt-dlp first — gives access to the highest available resolution
         (usually 720p HEVC for TikTok), with full width/height/duration
         metadata so Telegram does not re-encode.
      2) TikWM API as fallback — fast and reliable, handy if yt-dlp fails
         due to TikTok extractor changes. Returns 540p H.264.
    """
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


def send_video_by_url(chat_id: int, media: dict, reply_to: int = None):
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
    return tg("sendVideo", payload, timeout=20)


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
        res = send_video_by_url(chat_id, media, reply_to=msg_id)
        if not res or not res.get("ok"):
            # Fallback: send as document or plain link
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
