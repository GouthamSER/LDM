# bot.py

import os
import time
import threading
import shutil
import sys
import aria2p
from pyrogram import Client, idle

# --- Modular Imports ---
from plugins.commands import register_handlers, on_startup
try:
    from plugins.weblive import start_web_server_thread
    UVICORN_AVAILABLE = True
except ImportError:
    print("WARNING: Could not import web server module 'plugins.weblive'. Health check will not run.")
    UVICORN_AVAILABLE = False
# -----------------------

# ---- tee stdout/stderr into bot.log so /log can send it ----
class _Tee:
    def __init__(self, stream, fh):
        self._s, self._f = stream, fh
    def write(self, data):
        try:
            self._s.write(data)
            self._f.write(data)
            self._f.flush()
        except Exception:
            pass
    def flush(self):
        try:
            self._s.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._s, name)

try:
    _logfh = open("bot.log", "w", buffering=1)
    sys.stdout = _Tee(sys.stdout, _logfh)
    sys.stderr = _Tee(sys.stderr, _logfh)
except Exception:
    pass

# ================= CONFIG (single source of truth: config.py) =================
import config

if config.API_ID == 1234567 or config.API_HASH == "YOUR_API_HASH" or config.BOT_TOKEN == "YOUR_BOT_TOKEN":
    print("❌ API_ID / API_HASH / BOT_TOKEN are not set.\n"
          "   Copy config.sample.env to config.env, fill it in, and start again\n"
          "   (or set them as environment variables).")
    sys.exit(1)

API_ID       = config.API_ID
API_HASH     = config.API_HASH
BOT_TOKEN    = config.BOT_TOKEN

DOWNLOAD_DIR = os.path.abspath(config.DOWNLOAD_DIR)
ARIA2_PORT   = config.ARIA2_PORT
ARIA2_HOST   = config.ARIA2_HOST
ARIA2_SECRET = config.ARIA2_SECRET
HEALTH_PORT  = config.HEALTH_PORT
ENGINE_NAME  = config.ENGINE_NAME

# ================= CLEANUP =================
def cleanup():
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

cleanup()

# ================= ARIA2 =================
aria2 = aria2p.API(
    aria2p.Client(
        host=f"http://{ARIA2_HOST}",
        port=ARIA2_PORT,
        secret=ARIA2_SECRET
    )
)

# ================= GLOBAL STATE (Centralized) =================
# This dictionary will be passed to the command handlers module
GLOBAL_STATE = {
    "ACTIVE": {},  
    "DOWNLOAD_COUNT": 0,
    "UPLOAD_COUNT": 0,
    "TOTAL_DOWNLOAD_TIME": 0,
    "TOTAL_UPLOAD_TIME": 0,
    "DOWNLOAD_COUNTER": 1,
    "DOWNLOAD_DIR": DOWNLOAD_DIR,
    "ENGINE_NAME": ENGINE_NAME,
    "ARIA_VER": "?",
    "DASHBOARDS": {},   # chat_id -> {"msg": Message, "page": int, "running": bool}
}

def time_tracker():
    """Increments total time spent downloading or uploading."""
    while True:
        if GLOBAL_STATE["DOWNLOAD_COUNT"] > 0:
            GLOBAL_STATE["TOTAL_DOWNLOAD_TIME"] += 1
        if GLOBAL_STATE["UPLOAD_COUNT"] > 0:
            GLOBAL_STATE["TOTAL_UPLOAD_TIME"] += 1
        time.sleep(1)

# Start the time tracking thread
threading.Thread(target=time_tracker, daemon=True).start()

# ================= BOT CLIENT =================
_client_kwargs = dict(api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)
try:
    app = Client("aria2-leech-bot", max_concurrent_transmissions=config.MAX_TRANSMISSIONS, **_client_kwargs)
except TypeError:   # fork without that parameter
    app = Client("aria2-leech-bot", **_client_kwargs)

# Register handlers from the external module
register_handlers(app, aria2, GLOBAL_STATE)

if __name__ == "__main__":
    try:
        aria2.get_stats()
        try:
            ver = aria2.client.get_version()
            GLOBAL_STATE["ARIA_VER"] = ver.get("version", "?") if isinstance(ver, dict) else str(ver)
        except Exception:
            GLOBAL_STATE["ARIA_VER"] = "?"
        print(f"✅ Aria2 {GLOBAL_STATE['ARIA_VER']} connected on port {ARIA2_PORT}!\n")
    except Exception as e:
        print(f"❌ Aria2 not running! ({e}) Exiting.\n")
        exit(1)
    
    print(f"🤖 Bot is starting... (Upload Engine: {ENGINE_NAME})\n")

    # Store bot start time for uptime calculation
    app.start_time = time.time()
    
    # Start the Uvicorn/Starlette health check in a separate thread
    if UVICORN_AVAILABLE:
        start_web_server_thread(HEALTH_PORT)
    
    async def _main():
        await app.start()
        try:
            await on_startup(app)      # command menu + restart notice + saved auth
        except Exception as e:
            print(f"⚠️ startup hook failed: {e}")
        print("✅ Bot is up.")
        await idle()
        await app.stop()

    app.run(_main())
