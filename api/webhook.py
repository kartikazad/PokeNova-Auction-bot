"""
api/webhook.py — Vercel serverless entry point for PokeNova Auction Bot.

Instead of continuously polling Telegram (which needs an always-on
process, incompatible with Vercel's serverless model), this exposes an
HTTP endpoint. Telegram pushes each new update here directly via POST,
this function processes it, and then the function shuts down until the
next update arrives.

Deployed URL will be something like:
  https://your-project.vercel.app/api/webhook

After deploying, you must tell Telegram to send updates here — see
DEPLOY_VERCEL.md for the one-time setup command.
"""

import asyncio
import os
import sys

# Make sure the project root (one level up from /api) is importable, since
# Vercel's Python runtime executes this file as a standalone module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify
from telegram import Update

from bot import build_application  # noqa: E402

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Built once per warm serverless instance, reused across invocations of the
# same instance (Vercel keeps recently-used instances "warm" for a while).
_application = None
_initialized = False


def _get_application():
    global _application
    if _application is None:
        _application = build_application()
    return _application


async def _process(update_data: dict) -> None:
    global _initialized
    application = _get_application()
    if not _initialized:
        await application.initialize()
        _initialized = True
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)


@app.route("/api/webhook", methods=["POST"])
def webhook():
    # Verify the request actually came from Telegram, not a random POST.
    if WEBHOOK_SECRET:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_token != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "invalid secret token"}), 403

    update_data = request.get_json(force=True, silent=True)
    if not update_data:
        return jsonify({"ok": False, "error": "no update data"}), 400

    asyncio.run(_process(update_data))
    return jsonify({"ok": True})


@app.route("/api/webhook", methods=["GET"])
def health_check():
    # Lets you sanity-check the deployed URL in a browser.
    return jsonify({"ok": True, "message": "PokeNova Auction Bot webhook is live."})