#!/usr/bin/env python3
"""
StubFlip web dashboard server.
Run:  python server.py
Then open http://localhost:5000 in your browser.
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from bot import StubBot

app = Flask(__name__, static_folder=".")

CONFIG_FILE = Path("config.json")

# Shared bot state (read by the dashboard via /api/stats)
bot_state: dict = {
    "running":        False,
    "status":         "stopped",   # stopped | running | error
    "stubsEarned":    0,
    "tradesCompleted": 0,
    "startTime":      None,
    "log":            [],
    "tradeHistory":   [],
}

_bot:    StubBot | None  = None
_thread: threading.Thread | None = None
_lock   = threading.Lock()

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "profitMargin":      500,
    "activeHoursStart":  8,
    "activeHoursEnd":    23,
    "cardTypes": {
        "diamondEquipment": True,
        "liveSeries":       True,
        "sponsorships":     False,
    },
    "delayBetweenTrades": 30,
    "maxBudget":          100000,
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/stats")
def get_stats():
    uptime = 0
    if bot_state["startTime"]:
        uptime = int(time.time() - bot_state["startTime"])
    return jsonify({**bot_state, "uptime": uptime})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "Invalid JSON"}), 400
    save_config(data)
    return jsonify({"success": True})


@app.route("/api/start", methods=["POST"])
def start_bot():
    global _bot, _thread

    with _lock:
        if bot_state["running"]:
            return jsonify({"success": False, "message": "Bot already running"})

        config = load_config()
        bot_state.update({
            "running":         True,
            "status":          "running",
            "startTime":       time.time(),
            "stubsEarned":     0,
            "tradesCompleted": 0,
            "log":             [],
            "tradeHistory":    [],
        })

        _bot = StubBot(config, bot_state)
        _thread = threading.Thread(target=_run_bot, daemon=True)
        _thread.start()

    return jsonify({"success": True})


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    global _bot

    with _lock:
        if _bot:
            _bot.stop()
        bot_state["running"] = False
        bot_state["status"]  = "stopped"
        bot_state["startTime"] = None

    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Bot thread wrapper
# ---------------------------------------------------------------------------

def _run_bot():
    global _bot
    try:
        _bot.run()
    except Exception as e:
        bot_state["log"].insert(0, {
            "time":  datetime.now().strftime("%H:%M:%S"),
            "msg":   f"Fatal error: {e}",
            "level": "error",
        })
    finally:
        bot_state["running"] = False
        bot_state["status"]  = "stopped"
        bot_state["startTime"] = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        print("Created default config.json")

    print("StubFlip dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
