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


def _delete_webhook():
    """Polling and webhooks are mutually exclusive — clear any old webhook."""
    try:
        requests.post(
            f"{TELEGRAM_API}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10,
        )
    except Exception as exc:
        logger.warning(f"deleteWebhook failed: {exc}")


def _polling_loop():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — polling will not start.")
        return

    _delete_webhook()
    logger.info("Telegram long-polling started.")

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
                logger.warning(f"getUpdates returned: {data}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 1
            for update in data.get("result", []):
                offset = update["update_id"] + 1
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
