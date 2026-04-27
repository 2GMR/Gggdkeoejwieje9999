"""Hugging Face Spaces entry point.

Runs two things in parallel:
  1) A tiny Flask server on $PORT (default 7860) so HF Spaces sees the
     container as "alive" and the Space build succeeds.
  2) A background thread that long-polls Telegram for updates and
     dispatches them to the existing handler in api/webhook.py.

This avoids needing a public webhook, keep-alive pings, or any external
service. As long as polling is running, HF Spaces won't put the Space
to sleep — the container is constantly busy.
"""
import logging
import os
import sys
import threading
import time

import requests
from flask import Flask, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.webhook import handle_update, BOT_TOKEN, TELEGRAM_API  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("hf-app")

app = Flask(__name__)


@app.route("/")
def root():
    return jsonify(
        ok=True,
        bot="TikTok Downloader",
        mode="polling",
        message="Bot is running. Send a TikTok link on Telegram.",
    )


@app.route("/health")
def health():
    return jsonify(ok=True)


def _get_me():
    """Identify which bot this token actually belongs to."""
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
    """Polling and webhooks are mutually exclusive — clear any old webhook."""
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
        logger.error("Cannot identify bot. Check BOT_TOKEN secret.")
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
                # 401 = bad token, no point retrying.
                if data.get("error_code") == 401:
                    logger.error(
                        "BOT_TOKEN is invalid (401). "
                        "Update the secret in Space Settings and restart."
                    )
                    while True:
                        time.sleep(3600)
                # 409 = another instance is polling the same token.
                if data.get("error_code") == 409:
                    logger.error(
                        "Conflict (409): another bot instance is polling. "
                        "Stop the other instance (e.g. on Replit) and wait."
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


def _start_polling_thread():
    t = threading.Thread(target=_polling_loop, name="tg-polling", daemon=True)
    t.start()


_start_polling_thread()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, threaded=True)
