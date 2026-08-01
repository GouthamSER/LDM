# bot.py

import os
import time
import threading
import shutil
import aria2p
from pyrogram import Client

# --- Modular Imports ---
from plugins.commands import register_handlers
try:
    from plugins.weblive import start_web_server_thread
    UVICORN_AVAILABLE = True
except ImportError:
    print("WARNING: Could not import web server module 'plugins.weblive'. Health check will not run.")
    UVICORN_AVAILABLE = False
# -----------------------

# ================= CONFIG (single source of truth: config.py) =================
import config

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
app = Client(
    "aria2-leech-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16
)

# Register handlers from the external module
register_handlers(app, aria2, GLOBAL_STATE)

if __name__ == "__main__":
    try:
        aria2.get_stats()
        print("✅ Aria2 connected on port 6801!\n")
    except:
        print("❌ Aria2 not running! Exiting.\n")
        exit(1)
    
    print(f"🤖 Bot is starting... (Upload Engine: {ENGINE_NAME})\n")

    # Store bot start time for uptime calculation
    app.start_time = time.time()
    
    # Start the Uvicorn/Starlette health check in a separate thread
    if UVICORN_AVAILABLE:
        start_web_server_thread(HEALTH_PORT)
    
    app.run()
