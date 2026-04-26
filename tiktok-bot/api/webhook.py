import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
    "format": "best",
    "socket_timeout": 8,
    "retries": 1,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G960F) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        )
    },
    "extractor_args": {
        "tiktok": {
            "api_hostname": ["api22-normal-c-useast1a.tiktokv.com"],
            "app_name": ["musical_ly"],
            "app_version": ["34.1.2"],
            "manifest_app_version": ["2023401020"],
        }
    },
}

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


def _extract_via_tikwm(url: str) -> dict:
    """Primary extractor: TikWM API. Reliable, returns HD no-watermark URLs."""
    last_err = None
    for endpoint in TIKWM_ENDPOINTS:
        try:
            r = requests.post(
                endpoint,
                data={"url": url, "hd": "1"},
                headers=TIKWM_HEADERS,
                timeout=12,
            )
            if r.status_code != 200:
                last_err = f"http {r.status_code}"
                continue
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                last_err = data.get("msg") or "no data"
                continue

            d = data["data"]

            # Slideshow (images) — TikWM returns "images" array
            images = d.get("images") or []
            if images:
                return {
                    "type": "images",
                    "urls": list(images),
                    "title": d.get("title") or "",
                }

            # Video — prefer HD, then play (no watermark), then wmplay
            video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
            if video_url:
                return {
                    "type": "video",
                    "url": video_url,
                    "title": d.get("title") or "",
                    "duration": int(d.get("duration") or 0),
                    "width": int((d.get("size") or {}).get("width") or 0)
                    if isinstance(d.get("size"), dict)
                    else 0,
                    "height": int((d.get("size") or {}).get("height") or 0)
                    if isinstance(d.get("size"), dict)
                    else 0,
                    "thumbnail": d.get("cover") or d.get("origin_cover"),
                }
            last_err = "no video url"
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"tikwm: {last_err}")


def _extract_via_ytdlp(url: str) -> dict:
    """Fallback extractor: yt-dlp."""
    with YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
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

    video_url = None
    formats = info.get("formats") or []
    if formats:
        def _key(f):
            return (
                f.get("height") or 0,
                f.get("tbr") or 0,
                1 if (f.get("ext") == "mp4") else 0,
            )
        candidates = [f for f in formats if f.get("url") and f.get("vcodec") != "none"]
        if candidates:
            candidates.sort(key=_key, reverse=True)
            video_url = candidates[0].get("url")

    if not video_url:
        video_url = info.get("url")

    if video_url:
        return {
            "type": "video",
            "url": video_url,
            "title": info.get("title") or "",
            "duration": info.get("duration") or 0,
            "width": info.get("width") or 0,
            "height": info.get("height") or 0,
            "thumbnail": info.get("thumbnail"),
        }

    return {"type": "none"}


def extract_media(url: str) -> dict:
    """Extract direct media URLs without downloading.

    Strategy: try the reliable TikWM API first (fast, HD, no-watermark, supports
    slideshows). If that fails for any reason, fall back to yt-dlp.
    """
    try:
        result = _extract_via_tikwm(url)
        if result and result.get("type") in ("video", "images"):
            return result
    except Exception as e:
        logger.warning(f"TikWM extractor failed, falling back to yt-dlp: {e}")

    try:
        return _extract_via_ytdlp(url)
    except Exception as e:
        logger.error(f"yt-dlp extractor failed: {e}")
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
