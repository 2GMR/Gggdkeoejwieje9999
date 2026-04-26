"""Local entry point for testing on Replit. NOT used by Vercel.

Vercel routes traffic to api/webhook.py automatically via vercel.json.
This file just lets us run the same Flask app locally on $PORT.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.webhook import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
