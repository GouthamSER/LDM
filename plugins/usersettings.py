# plugins/usersettings.py
"""
Per-user settings: upload type (document / video) + custom thumbnail.

Storage: MongoDB when config.DATABASE_URL is set (motor), otherwise a local
JSON file. Thumbnails are kept as bytes in Mongo and cached on disk, because
Telegram needs a real file path for `thumb=`.
"""

import asyncio
import io
import json
import os
import shutil

import config

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

THUMB_DIR = os.path.abspath("thumbs")
COOKIE_DIR = os.path.abspath("cookies")
JSON_PATH = os.path.abspath("userdata.json")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".3gp")

_col = None
_col_failed = False
_cache = {}          # uid -> {"mode": "doc"|"video", "thumb": path|None}
_json_lock = asyncio.Lock()


def backend():
    return "MongoDB" if (config.DATABASE_URL and AsyncIOMotorClient and not _col_failed) else "JSON file"


def _collection():
    global _col, _col_failed
    if _col is not None or _col_failed:
        return _col
    if config.DATABASE_URL and AsyncIOMotorClient:
        try:
            _col = AsyncIOMotorClient(config.DATABASE_URL, serverSelectionTimeoutMS=8000)[config.DATABASE_NAME]["users"]
        except Exception as e:
            print(f"⚠️ MongoDB connect failed, using JSON file: {e}")
            _col_failed = True
    return _col


def _thumb_path(uid):
    return os.path.join(THUMB_DIR, f"{uid}.jpg")


def _cookie_path(uid):
    return os.path.join(COOKIE_DIR, f"{uid}.txt")


def is_video(path):
    return path.lower().endswith(VIDEO_EXTS)


# ───────── load / save ─────────

async def _json_read():
    try:
        return await asyncio.to_thread(lambda: json.load(open(JSON_PATH)))
    except Exception:
        return {}


async def _json_write(data):
    await asyncio.to_thread(lambda: json.dump(data, open(JSON_PATH, "w")))


async def get(uid):
    """-> {"mode": "doc"|"video", "thumb": path|None}"""
    if uid in _cache:
        return _cache[uid]
    mode, thumb, dump = "doc", None, 0
    rules = {"prefix": "", "suffix": "", "name_swap": "", "caption": ""}
    cookie, use_def = None, False
    col = _collection()
    try:
        if col is not None:
            d = await col.find_one({"_id": uid})
            if d:
                mode = d.get("mode", "doc")
                dump = int(d.get("dump", 0) or 0)
                rules = {k: d.get(k, "") or "" for k in rules}
                use_def = bool(d.get("use_default_cookie", False))
                if d.get("cookies"):
                    os.makedirs(COOKIE_DIR, exist_ok=True)
                    cp = _cookie_path(uid)
                    await asyncio.to_thread(lambda: open(cp, "wb").write(d["cookies"]))
                    cookie = cp
                if d.get("thumb"):
                    os.makedirs(THUMB_DIR, exist_ok=True)
                    p = _thumb_path(uid)
                    await asyncio.to_thread(lambda: open(p, "wb").write(d["thumb"]))
                    thumb = p
        else:
            async with _json_lock:
                rec = (await _json_read()).get(str(uid), {})
            mode, dump = rec.get("mode", "doc"), int(rec.get("dump", 0) or 0)
            rules = {k: rec.get(k, "") or "" for k in rules}
            use_def = bool(rec.get("use_default_cookie", False))
            if os.path.exists(_cookie_path(uid)):
                cookie = _cookie_path(uid)
            if os.path.exists(_thumb_path(uid)):
                thumb = _thumb_path(uid)
    except Exception as e:
        print(f"⚠️ settings load failed for {uid}: {e}")
    _cache[uid] = {"mode": mode, "thumb": thumb, "dump": dump, "cookie": cookie,
                   "use_default_cookie": use_def, **rules}
    return _cache[uid]


async def _save(uid, **fields):
    col = _collection()
    try:
        if col is not None:
            await col.update_one({"_id": uid}, {"$set": fields}, upsert=True)
        else:
            async with _json_lock:
                data = await _json_read()
                data.setdefault(str(uid), {}).update({k: v for k, v in fields.items() if k != "thumb"})
                await _json_write(data)
    except Exception as e:
        print(f"⚠️ settings save failed for {uid}: {e}")


async def set_dump(uid, chat_id):
    """chat_id 0 = clear (upload back into the command chat)."""
    (await get(uid))["dump"] = int(chat_id)
    await _save(uid, dump=int(chat_id))


async def set_rule(uid, key, value):
    """key in prefix | suffix | name_swap | caption. Empty value clears it."""
    assert key in ("prefix", "suffix", "name_swap", "caption")
    (await get(uid))[key] = value
    await _save(uid, **{key: value})


async def set_mode(uid, mode):
    s = await get(uid)
    s["mode"] = mode
    await _save(uid, mode=mode)


def _to_thumb_jpeg(raw: bytes) -> bytes:
    """Telegram thumb rules: JPEG, <=200KB, sides <=320px."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((320, 320))
    q = 90
    while True:
        out = io.BytesIO()
        im.save(out, "JPEG", quality=q)
        if out.tell() <= 200 * 1024 or q <= 30:
            return out.getvalue()
        q -= 10


async def set_thumb(uid, raw: bytes):
    jpg = await asyncio.to_thread(_to_thumb_jpeg, raw)
    os.makedirs(THUMB_DIR, exist_ok=True)
    p = _thumb_path(uid)
    await asyncio.to_thread(lambda: open(p, "wb").write(jpg))
    (await get(uid))["thumb"] = p
    col = _collection()
    if col is not None:
        await _save(uid, thumb=jpg)


async def del_thumb(uid):
    s = await get(uid)
    s["thumb"] = None
    try:
        os.remove(_thumb_path(uid))
    except OSError:
        pass
    col = _collection()
    if col is not None:
        try:
            await col.update_one({"_id": uid}, {"$unset": {"thumb": ""}})
        except Exception as e:
            print(f"⚠️ thumb delete failed: {e}")


# ───────── ffmpeg helpers ─────────

async def _run(cmd, timeout=40):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        return b""


async def probe_video(path):
    """-> (duration_sec, width, height); zeros if ffprobe missing/fails."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0, 0, 0
    out = await _run([ffprobe, "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=width,height:format=duration", "-of", "json", path])
    try:
        d = json.loads(out)
        st = (d.get("streams") or [{}])[0]
        return float(d.get("format", {}).get("duration", 0) or 0), int(st.get("width", 0)), int(st.get("height", 0))
    except Exception:
        return 0, 0, 0


async def make_thumb(path, duration):
    """Grab a frame at ~10% of the video; returns jpg path or None."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    out = f"{path}.thumb.jpg"
    ss = max(1, int(duration * 0.1)) if duration else 1
    await _run([ffmpeg, "-y", "-loglevel", "error", "-ss", str(ss), "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out])
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None


# ───────── persisted /authorize list ─────────

async def load_auth():
    """-> (users[], chats[]) saved by /authorize."""
    col = _collection()
    try:
        if col is not None:
            d = await col.find_one({"_id": "auth_cfg"}) or {}
            return list(d.get("users", [])), list(d.get("chats", []))
        async with _json_lock:
            d = (await _json_read()).get("__auth__", {})
        return list(d.get("users", [])), list(d.get("chats", []))
    except Exception as e:
        print(f"⚠️ auth load failed: {e}")
        return [], []


async def save_auth(users, chats):
    col = _collection()
    try:
        if col is not None:
            await col.update_one({"_id": "auth_cfg"}, {"$set": {"users": list(users), "chats": list(chats)}}, upsert=True)
        else:
            async with _json_lock:
                data = await _json_read()
                data["__auth__"] = {"users": list(users), "chats": list(chats)}
                await _json_write(data)
    except Exception as e:
        print(f"⚠️ auth save failed: {e}")


# ───────── yt-dlp cookies (Leechv2 logic) ─────────
# * personal cookies per user (stored in MongoDB, cached as cookies/<uid>.txt)
# * one default cookies file for the whole bot (owner), written to config.YTDL_COOKIES
# * a user with personal cookies uses them unless they toggled "use default"

def valid_cookies(data: bytes):
    """'' if it looks like a Netscape cookies.txt, else an error text."""
    if len(data) > 1_500_000:
        return "file too big for a cookies.txt (>1.5MB)"
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return "not a text file"
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if "Netscape" in text[:200] or any(len(l.split("\t")) >= 7 for l in lines):
        return ""
    return "not a Netscape cookies.txt (export it with a 'Get cookies.txt' browser extension)"


async def set_cookie(uid, data: bytes):
    os.makedirs(COOKIE_DIR, exist_ok=True)
    p = _cookie_path(uid)
    await asyncio.to_thread(lambda: open(p, "wb").write(data))
    s = await get(uid)
    s["cookie"] = p
    s["use_default_cookie"] = False
    col = _collection()
    if col is not None:
        await _save(uid, cookies=data, use_default_cookie=False)
    else:
        await _save(uid, use_default_cookie=False)


async def del_cookie(uid):
    s = await get(uid)
    s["cookie"] = None
    try:
        os.remove(_cookie_path(uid))
    except OSError:
        pass
    col = _collection()
    if col is not None:
        try:
            await col.update_one({"_id": uid}, {"$unset": {"cookies": ""}})
        except Exception as e:
            print(f"⚠️ cookie delete failed: {e}")


async def set_use_default(uid, flag: bool):
    (await get(uid))["use_default_cookie"] = flag
    await _save(uid, use_default_cookie=flag)


def default_cookie_exists():
    return bool(config.YTDL_COOKIES) and os.path.exists(config.YTDL_COOKIES)


async def set_default_cookie(data: bytes):
    await asyncio.to_thread(lambda: open(config.YTDL_COOKIES, "wb").write(data))
    col = _collection()
    if col is not None:
        try:
            await col.update_one({"_id": "cookies_default"}, {"$set": {"data": data}}, upsert=True)
        except Exception as e:
            print(f"⚠️ default cookie save failed: {e}")


async def del_default_cookie():
    try:
        os.remove(config.YTDL_COOKIES)
    except OSError:
        pass
    col = _collection()
    if col is not None:
        try:
            await col.delete_one({"_id": "cookies_default"})
        except Exception as e:
            print(f"⚠️ default cookie delete failed: {e}")


async def load_default_cookie():
    """Startup: restore the owner's default cookies.txt from MongoDB (the disk is ephemeral)."""
    col = _collection()
    if col is None:
        return False
    try:
        d = await col.find_one({"_id": "cookies_default"})
        if d and d.get("data"):
            await asyncio.to_thread(lambda: open(config.YTDL_COOKIES, "wb").write(d["data"]))
            return True
    except Exception as e:
        print(f"⚠️ default cookie load failed: {e}")
    return False


async def cookie_for(uid):
    """-> (path|None, label). Personal cookies win unless 'use default' is on."""
    s = await get(uid)
    if s["cookie"] and not s["use_default_cookie"] and os.path.exists(s["cookie"]):
        return s["cookie"], "personal"
    if default_cookie_exists():
        return config.YTDL_COOKIES, "default"
    return None, "none"
