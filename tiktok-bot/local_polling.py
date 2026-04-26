"""Local testing entry point that uses Telegram long-polling.

This file is ONLY for testing on Replit (where exposing a public webhook
is inconvenient). On Vercel, the bot uses webhooks via api/webhook.py.

Both modes share the same media-handling logic from api/webhook.py.
"""
import os
import sys
import time
import logging
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reuse the same handler from the webhook module so behavior matches Vercel.
from api.webhook import handle_update, BOT_TOKEN, TELEGRAM_API  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("polling")


def delete_webhook():
    """Make sure no webhook is registered, otherwise getUpdates returns 409."""
    try:
        r = requests.post(
            f"{TELEGRAM_API}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10,
        )
        logger.info(f"deleteWebhook: {r.json()}")
    except Exception as e:
        logger.warning(f"deleteWebhook failed: {e}")


def get_me():
    try:
        r = requests.get(f"{TELEGRAM_API}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            u = data["result"]
            logger.info(f"Bot: @{u.get('username')}  id={u.get('id')}")
            return u
        logger.error(f"getMe error: {data}")
    except Exception as e:
        logger.error(f"getMe failed: {e}")
    return None


def poll():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Aborting.")
        sys.exit(1)

    me = get_me()
    if not me:
        logger.error("Cannot reach Telegram. Check BOT_TOKEN.")
        sys.exit(1)

    delete_webhook()

    offset = 0
    logger.info("Polling started. Send a TikTok link to your bot in Telegram.")

    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": '["message","edited_message"]',
                },
                timeout=35,
            )
            data = r.json()
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            logger.error(f"getUpdates error: {e}")
            time.sleep(2)
            continue

        if not data.get("ok"):
            logger.error(f"Telegram error: {data}")
            time.sleep(2)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            try:
                logger.info(f"Update {update['update_id']}")
                handle_update(update)
            except Exception:
                logger.exception("handle_update failed")


if __name__ == "__main__":
    poll()
