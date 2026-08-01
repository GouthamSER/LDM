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
