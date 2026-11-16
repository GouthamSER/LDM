import os

def get_env_variable(var_name, default_value=None):
    return os.environ.get(var_name, default_value)

# =================
# Telegram Credentials
# =================
API_ID = int(get_env_variable("API_ID", 1234567)) 
API_HASH = get_env_variable("API_HASH", "YOUR_API_HASH") 
BOT_TOKEN = get_env_variable("BOT_TOKEN", "YOUR_BOT_TOKEN") 
OWNER_ID = int(get_env_variable("OWNER_ID", 123456789))

# =================
# Aria2 RPC Configuration
# =================
ARIA2_HOST = get_env_variable("ARIA2_HOST", "127.0.0.1")
ARIA2_PORT = int(get_env_variable("ARIA2_PORT", 6801)) # DONT CHANGEEE !!!!
ARIA2_SECRET = get_env_variable("ARIA2_SECRET", "gjxdml")

# --- Aria2 Host Check ---
# Simple validation: Ensure ARIA2_HOST is not empty and is a valid IP or hostname
import ipaddress
try:
    ipaddress.ip_address(ARIA2_HOST)  # Check if it's a valid IP
except ValueError:
    if not ARIA2_HOST or not isinstance(ARIA2_HOST, str):
        raise ValueError("Invalid ARIA2_HOST: Must be a valid IP address or hostname.")
# Note: For full hostname validation, you could use socket.gethostbyname(ARIA2_HOST) but it requires network access.
# If you want a runtime connection test, add it in bot.py after aria2 setup.

# =================
# Upload Engine (Pyrogram-compatible fork)
# =================
# Two selectable engines, toggled in requirements.txt:
#   pyroblack   (default)
#   wzgram[fast] (rust WarpCrypto backend, faster)
# Both import as `pyrogram`, so code never changes — only
# requirements.txt needs a comment swap. Label below is
# auto-detected from whichever is actually installed;
# ENGINE_NAME env var can still force an override.
def _detect_engine():
    try:
        from importlib import metadata
        for pkg, label in (("wzgram", "wzgram[fast]"), ("pyroblack", "Pyroblack")):
            try:
                metadata.version(pkg)
                return label
            except metadata.PackageNotFoundError:
                continue
    except Exception:
        pass
    return "Pyroblack"

ENGINE_NAME = get_env_variable("ENGINE_NAME") or _detect_engine()

# =================
# Bot Operational Settings
# =================
DOWNLOAD_DIR = get_env_variable("DOWNLOAD_DIR", "./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
SESSION_NAME = get_env_variable("SESSION_NAME", "ariadwd")

# Web/Health Check Port (used in the provided bot.py and plugins.weblive)
HEALTH_PORT = int(get_env_variable("PORT", 8000))

# =================
# qBittorrent (WebUI, local qbittorrent-nox started by start.sh)
# =================
QBIT_HOST     = get_env_variable("QBIT_HOST", "127.0.0.1")
QBIT_PORT     = int(get_env_variable("QBIT_PORT", 8090))
QBIT_USERNAME = get_env_variable("QBIT_USERNAME", "")   # empty = localhost auth bypass (start.sh default)
QBIT_PASSWORD = get_env_variable("QBIT_PASSWORD", "")

# =================
# Access control
# =================
# OWNER_ID is always allowed. Add more users / groups as comma lists.
# While OWNER_ID is still the 123456789 placeholder the bot stays open to
# everyone (so a fresh deploy works); set a real OWNER_ID to lock it down.
def _id_list(name):
    out = []
    for x in str(get_env_variable(name, "")).replace(" ", "").split(","):
        try:
            out.append(int(x))
        except ValueError:
            pass
    return out

AUTH_USERS = _id_list("AUTH_USERS")
AUTH_CHATS = _id_list("AUTH_CHATS")

# =================
# Upload behaviour
# =================
# Parallel Telegram uploads
# Max files uploading at once across ALL tasks. 0 = no cap (Leechv2 style: each task uploads its
# files one by one, tasks run in parallel and share the bandwidth).
MAX_UPLOADS = int(get_env_variable("MAX_UPLOADS", 0))
# Parallel file-part streams per upload inside the Telegram client (1 = safest vs FLOOD_WAIT)
MAX_TRANSMISSIONS = int(get_env_variable("MAX_TRANSMISSIONS", 1))
# Files bigger than this are split into .001 .002 ... parts (bytes).
# Default 1.95GiB (safe for the 2GB bot limit). Premium uploader: 3.9GiB. 0 = never split.
SPLIT_SIZE = int(get_env_variable("SPLIT_SIZE", int(1.95 * 1024**3)))
# Default caption (per-user override: /setcaption). Placeholders: {filename} {size} {duration}
# {quality} {languages} {subtitles} {md5_hash}. HTML allowed. Lines using video-only
# placeholders are dropped for non-video files.
LEECH_CAPTION = get_env_variable(
    "LEECH_CAPTION",
    "<b><i>{filename}</i></b>\n\nSize🎲 : <i>{size}</i>\nQuality 💡: <i>{quality}</i>\n"
    "Lang 🧩: <i>{languages}</i>\nSub 📄: <i>{subtitles}</i>")
# Leech into this chat instead of the command chat (0 = same chat). Optional dump channel.
LEECH_DUMP = int(get_env_variable("LEECH_DUMP", 0))

# =================
# MongoDB (per-user settings: upload type + thumbnail). Optional.
# Empty = settings fall back to a local JSON file (lost on ephemeral hosts like Koyeb).
# =================
DATABASE_URL  = get_env_variable("DATABASE_URL", "")
DATABASE_NAME = get_env_variable("DATABASE_NAME", "ldmbot")

# =================
# Filename rules (ported from Leechv2). Per-user values (/setswap /setprefix /setsuffix) win over these.
# NAME_SWAP format:  pattern:replacement|pattern2:replacement2[:count]
#   e.g.  \[.*?\]::|www\.\S+::     (remove [brackets] and www urls)
# ("\s" in a pattern/prefix/suffix is turned into a space)
# =================
LEECH_PREFIX    = get_env_variable("LEECH_PREFIX", "")
LEECH_SUFFIX    = get_env_variable("LEECH_SUFFIX", "")
LEECH_NAME_SWAP = get_env_variable("LEECH_NAME_SWAP", "")

# =================
# yt-dlp
# =================
# Netscape cookies file (YouTube sign-in / age gate etc). Used when the file exists.
YTDL_COOKIES = get_env_variable("YTDL_COOKIES", "cookies.txt")
# Extra yt-dlp options as a python-dict literal, e.g. {"proxy": "http://..."}
YTDL_OPTIONS = get_env_variable("YTDL_OPTIONS", "")

# =================
# Auto update on every start / restart (update.py). Empty UPSTREAM_REPO = no update.
# Private repo: https://<token>@github.com/user/repo
# =================
UPSTREAM_REPO = get_env_variable("UPSTREAM_REPO", "")
UPSTREAM_BRANCH = get_env_variable("UPSTREAM_BRANCH", "main")
UPDATE_KEEP_FILES = get_env_variable("UPDATE_KEEP_FILES", "")   # e.g. "config.py,cookies.txt"

# =================
# Telegram command menu (the "/" list) — set automatically on every start
# =================
SET_COMMANDS = str(get_env_variable("SET_COMMANDS", "True")).lower() in ("1", "true", "yes")
