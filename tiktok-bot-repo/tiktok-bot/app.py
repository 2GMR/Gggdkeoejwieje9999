"""Universal entry point for the TikTok bot.

Modes (selected via the BOT_MODE env var):

  * BOT_MODE=webhook (default)
      Runs the Flask app only. Telegram pushes updates to /api/webhook.
      Use this on Render, Railway, Fly, or any platform that gives you a
      public HTTPS URL. After deploy, visit /api/setwebhook once to
      register the webhook with Telegram.

  * BOT_MODE=polling
      Runs the Flask app AND a background thread that long-polls Telegram
      for updates. Use this on Replit, local dev, or any environment that
      cannot expose a public webhook URL.

Either way the same Flask app from api/webhook.py is used, so the routes
(/, /api/webhook, /api/setwebhook) work in both modes.
"""
import logging
import os
import sys
import threading
import time

import requests
from flask import jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.webhook import app, handle_update, BOT_TOKEN, TELEGRAM_API  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bot-app")

BOT_MODE = os.getenv("BOT_MODE", "webhook").strip().lower()

# Self-ping interval (seconds). Render free tier sleeps after 15 min of
# inactivity, so we ping every 10 min to stay awake forever.
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "600"))
KEEPALIVE_ENABLED = os.getenv("KEEPALIVE", "1").strip().lower() not in ("0", "false", "no", "off")


@app.route("/health")
def health():
    return jsonify(ok=True, mode=BOT_MODE)


def _keepalive_loop():
    """Background self-ping to prevent the free-tier instance from sleeping.

    Hits our own /health endpoint every KEEPALIVE_INTERVAL seconds. Any
    inbound HTTP request resets Render's idle timer, so the service stays
    awake indefinitely even with zero user traffic.
    """
    base = (os.getenv("WEBHOOK_URL") or "").rstrip("/")
    if not base:
        logger.warning("KEEPALIVE: WEBHOOK_URL not set — self-ping disabled.")
        return
    target = base + "/health"
    logger.info(f"KEEPALIVE: pinging {target} every {KEEPALIVE_INTERVAL}s")
    # Initial delay so the server is fully bound before the first ping.
    time.sleep(30)
    while True:
        try:
            r = requests.get(target, timeout=20)
            logger.info(f"KEEPALIVE: ping ok ({r.status_code})")
        except Exception as exc:
            logger.warning(f"KEEPALIVE: ping failed: {exc}")
        time.sleep(KEEPALIVE_INTERVAL)


def _get_me():
    for attempt in range(3):
        try:
            r = requests.get(f"{TELEGRAM_API}/getMe", timeout=15)
            data = r.json()
            if data.get("ok"):
                u = data["result"]
                logger.info(
                    f"Connected to bot: @{u.get('username')} "
                    f"(id={u.get('id')}, name={u.get('first_name')!r})"
                )
                return u
            logger.error(f"getMe failed: {data}")
            return None
        except Exception as exc:
            logger.warning(f"getMe attempt {attempt+1}/3 failed: {exc}")
            time.sleep(3)
    return None


def _delete_webhook():
    for attempt in range(3):
        try:
            r = requests.post(
                f"{TELEGRAM_API}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=15,
            )
            data = r.json()
            logger.info(f"deleteWebhook: {data}")
            return
        except Exception as exc:
            logger.warning(f"deleteWebhook attempt {attempt+1}/3 failed: {exc}")
            time.sleep(3)


def _polling_loop():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — polling will not start.")
        return

    me = _get_me()
    if not me:
        logger.error("Cannot identify bot. Check BOT_TOKEN and outbound network access.")
        return

    _delete_webhook()
    logger.info("Telegram long-polling started. Send a TikTok link on Telegram.")

    offset = 0
    backoff = 1
    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": 50, "offset": offset},
                timeout=60,
            )
            data = r.json()
            if not data.get("ok"):
                if data.get("error_code") == 401:
                    logger.error(
                        "BOT_TOKEN is invalid (401). Update the secret and restart."
                    )
                    while True:
                        time.sleep(3600)
                if data.get("error_code") == 409:
                    logger.error(
                        "Conflict (409): another bot instance is polling. "
                        "Stop the other instance and wait."
                    )
                logger.warning(f"getUpdates returned: {data}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 1
            updates = data.get("result", [])
            if updates:
                logger.info(f"Received {len(updates)} update(s)")
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message") or {}
                text_preview = (msg.get("text") or "")[:80]
                logger.info(
                    f"Update {update['update_id']} from "
                    f"chat={msg.get('chat',{}).get('id')} text={text_preview!r}"
                )
                try:
                    handle_update(update)
                except Exception as exc:
                    logger.exception(f"handler error: {exc}")
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as exc:
            logger.warning(f"polling loop error: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if BOT_MODE == "polling":
    logger.info("Starting in POLLING mode (background thread).")
    threading.Thread(target=_polling_loop, name="tg-polling", daemon=True).start()
else:
    logger.info(
        "Starting in WEBHOOK mode. After deploy, visit /api/setwebhook "
        "with WEBHOOK_URL set to your public HTTPS URL to register."
    )
    if KEEPALIVE_ENABLED:
        threading.Thread(target=_keepalive_loop, name="keepalive", daemon=True).start()
    else:
        logger.info("KEEPALIVE: disabled via env var.")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, threaded=True)
