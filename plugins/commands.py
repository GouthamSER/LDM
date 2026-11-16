# plugins/commands.py

import os
import sys
import json
import time
import asyncio
import html as _html

# NOTE: We need InlineKeyboardMarkup and InlineKeyboardButton for /status
from pyrogram import filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from pyrogram.errors import FloodWait

import shutil
from pathlib import Path
import config
from plugins import qbit as qb
from plugins import usersettings as us
from plugins import ytdl
from plugins.caption_utils import build_caption, validate_template
from plugins.filename_utils import format_filename, has_rules, validate_swap

# ================= Global State and Helpers (Placeholders to be set by bot.py) =================
GLOBAL_STATE = {}
ARIA2_API = None
BOT_START = time.time()   # pyrogram Client has no start_time; uptime counts from import

# Uploads work like Leechv2: every task uploads its own files ONE AT A TIME, and all tasks
# upload at the same time (bandwidth is shared between them). MAX_UPLOADS > 0 optionally caps
# how many files upload at once across the whole bot (0 = no cap).
UPLOAD_SEMAPHORE = asyncio.Semaphore(config.MAX_UPLOADS) if config.MAX_UPLOADS > 0 else None


class _NoSlot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

# --- Helper Functions ---

def progress_bar(done, total, size=13):
    # Matches the WZML-X-style reference look: solid filled cells, one
    # distinct "leading edge" cell, rest empty.
    FILLED, CURRENT, EMPTY = "■", "▨", "░"
    if total == 0:
        return f"[{EMPTY * size}] 0.00%"
    percent = min(100.0, (done / total) * 100)
    filled = int(percent / 100 * size)
    if filled >= size:
        bar = FILLED * size
    elif percent <= 0:
        bar = EMPTY * size
    else:
        bar = FILLED * filled + CURRENT + EMPTY * (size - filled - 1)
    return f"[{bar}] {percent:.2f}%"

def time_fmt(sec):
    if not isinstance(sec, (int, float)): sec = 0
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}h{m}m{s}s"
    elif m > 0: return f"{m}m{s}s"
    else: return f"{s}s"

def format_speed(bps):
    if bps == 0: return "0B/s"
    units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s', 'PB/s']
    unit = 0
    while bps >= 1024 and unit < len(units) - 1:
        bps /= 1024
        unit += 1
    return f"{bps:.1f}{units[unit]}"

def format_size(b): 
    if b == 0: return "0B"
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    unit = 0
    while b >= 1024 and unit < len(units) - 1:
        b /= 1024
        unit += 1
    return f"{b:.2f}{units[unit]}"

# --- Filename Cleaner ---
# Strips site-tag junk like "[www.xxxmovies.xyz] Movie.mkv" or
# "www.xxx.com - Movie.mkv" down to just "Movie.mkv".
import re as _re
_SITE_TAG_RE = _re.compile(
    r'^\s*[\[\(]?\s*(?:www\.)?[\w-]+\.(?:xyz|com|net|org|info|cc|to|me|in|co|live|link|club|'
    r'site|online|fun|icu|mx|pw|sx|vip|cam|gg|click|download|mov|pro|biz|asia|tv|ws)'
    r'(?:\.[a-z]{2,4})?\s*[\]\)]?\s*[-_.:|]*\s*',
    _re.IGNORECASE
)

def clean_filename(name):
    base, ext = os.path.splitext(name)
    cleaned = _SITE_TAG_RE.sub('', base, count=1).strip(" -_.")
    if not cleaned:
        cleaned = base.strip(" -_.") or base
    return f"{cleaned}{ext}"


def _extract_rename_flag(args):
    """Pull `-n <new name>` (rest of args joined, so spaces work) out of an arg list."""
    if "-n" not in args:
        return args, None
    idx = args.index("-n")
    rename_to = " ".join(args[idx + 1:]).strip() or None
    return args[:idx], rename_to

def _parse_multi(args):
    """Pull `-i N` (multi: this message and the next N-1) out of an arg list -> (args, n|0)."""
    if "-i" not in args:
        return args, 0
    i = args.index("-i")
    n = 0
    if i + 1 < len(args) and args[i + 1].isdigit():
        n = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    else:
        args = args[:i] + args[i + 1:]
        n = 1
    return args, max(n, 0)


def _parse_zip(args):
    """Pull `-z [password]` out of an arg list -> (args, None | password-or-"").
    The password is the token after -z unless it is another flag or a link."""
    if "-z" not in args:
        return args, None
    i = args.index("-z")
    pw = ""
    if i + 1 < len(args) and not args[i + 1].startswith("-") and not _URL_RE.match(args[i + 1]):
        pw = args[i + 1]
        return args[:i] + args[i + 2:], pw
    return args[:i] + args[i + 1:], pw


def _tg_media(msg):
    """(media_obj, filename) for a Telegram message that carries a file, else (None, None)."""
    if not msg or getattr(msg, "empty", False):
        return None, None
    for attr, ext in (("document", ""), ("video", ".mp4"), ("audio", ".mp3"), ("animation", ".mp4"),
                      ("voice", ".ogg"), ("video_note", ".mp4"), ("photo", ".jpg")):
        obj = getattr(msg, attr, None)
        if obj:
            name = getattr(obj, "file_name", None) or f"{attr}_{msg.id}{ext}"
            return obj, name.replace("/", "_")
    return None, None


# --- Async Helper Functions ---

async def edit_message_async(msg, content, parse_mode, max_retries=3, reply_markup=None):
    if msg.text == content and not reply_markup: return None
    retries = 0
    while retries < max_retries:
        try:
            return await msg.edit(content, parse_mode=parse_mode, reply_markup=reply_markup)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            retries += 1
        except Exception:
            return None
    return None

async def reply_message_async(m, text, parse_mode=None, max_retries=3, reply_markup=None):
    retries = 0
    while retries < max_retries:
        try:
            return await m.reply(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            retries += 1
        except Exception:
            return None
    return None

# --- 7zip Extraction Helper ---

async def extract_file(
    file_path, extract_dir,
    msg, name, download_index, user_first, user_id, loop,
    progress_gid=None
) -> tuple:
    """
    Extracts a .7z or .zip archive using py7zr with live progress UI.
    Supports: .7z, .zip
    Returns (success: bool, message: str)
    """
    import py7zr
    import zipfile
    import tarfile

    fp         = str(file_path)
    ed         = str(extract_dir)
    lower      = fp.lower()
    start_time = time.time()
    TAR_EXTS   = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")

    def _send_progress(done, total):
        """Push live extraction stats into the dashboard (called from worker thread)."""
        elapsed = time.time() - start_time
        speed   = done / elapsed if elapsed > 0 else 0
        entry   = GLOBAL_STATE["ACTIVE"].get(progress_gid) if progress_gid else None
        if entry is not None:
            if entry.get("cancel"):
                raise Exception("Extraction cancelled by user.")
            entry["progress"] = {"done": done, "total": total, "speed": speed}

    # ── .7z extraction with py7zr callback ────────────────────────────────
    if lower.endswith(".7z"):
        def _extract_7z():
            # Get total uncompressed size upfront
            with py7zr.SevenZipFile(fp, mode="r") as arc:
                total = sum(
                    (i.uncompressed or 0)
                    for i in arc.list()
                )

            done_bytes = [0]
            last_edit  = [0.0]

            class _ProgressCB(py7zr.callbacks.ExtractCallback):
                def report_start_preparation(self): pass
                def report_postprocess(self):        pass
                def report_warning(self, w):         pass
                def report_update(self, decompressed_bytes): pass   # required by py7zr >= 1.0

                def report_start(self, path, size):
                    pass  # size = compressed chunk size, not useful here

                def report_end(self, path, wrote_bytes):
                    try:
                        done_bytes[0] += int(wrote_bytes)   # py7zr >= 1.0 passes a str
                    except (TypeError, ValueError):
                        pass
                    now = time.time()
                    if now - last_edit[0] >= 3:
                        last_edit[0] = now
                        _send_progress(done_bytes[0], total)

            with py7zr.SevenZipFile(fp, mode="r") as arc:
                arc.extractall(path=ed, callback=_ProgressCB())

            return True, "Extracted with py7zr."

        try:
            return await asyncio.to_thread(_extract_7z)
        except Exception as e:
            return False, str(e)

    # ── .zip extraction (file-by-file progress) ────────────────────────────
    elif lower.endswith(".zip"):
        def _extract_zip():
            with zipfile.ZipFile(fp, "r") as zf:
                infos     = zf.infolist()
                total     = sum(i.file_size for i in infos)
                done_bytes = 0
                last_edit  = 0.0

                for info in infos:
                    zf.extract(info, ed)
                    done_bytes += info.file_size
                    now = time.time()
                    if now - last_edit >= 3:
                        last_edit = now
                        _send_progress(done_bytes, total)

            return True, "Extracted with zipfile."

        try:
            return await asyncio.to_thread(_extract_zip)
        except Exception as e:
            return False, str(e)

    # ── .tar / .tar.gz / .tgz / .tar.bz2 / .tbz2 / .tar.xz / .txz ──────────
    elif lower.endswith(TAR_EXTS):
        def _extract_tar():
            with tarfile.open(fp, "r:*") as tf:
                members    = tf.getmembers()
                total      = sum(m.size for m in members)
                done_bytes = 0
                last_edit  = 0.0

                base = os.path.realpath(ed)
                for m in members:
                    dest = os.path.realpath(os.path.join(ed, m.name))
                    if not (dest == base or dest.startswith(base + os.sep)):
                        continue  # path traversal ("../") — skip, never write outside
                    tf.extract(m, ed)
                    done_bytes += m.size
                    now = time.time()
                    if now - last_edit >= 3:
                        last_edit = now
                        _send_progress(done_bytes, total)

            return True, "Extracted with tarfile."

        try:
            return await asyncio.to_thread(_extract_tar)
        except Exception as e:
            return False, str(e)

    else:
        return False, (
            f"Unsupported format: `{os.path.basename(fp)}`\n"
            "Supported: `.7z` `.zip` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tbz2` `.tar.xz` `.txz`\n"
            "(`.rar` needs the `unrar` binary — not bundled, add it yourself if needed)"
        )


# ================= ACCESS CONTROL =================

_OWNER_PLACEHOLDER = 123456789


def _split_mode(mode_tag):
    """'#Leech | #Aria2' -> (out_mode, in_mode)  (Leechv2 shows them as two lines)."""
    parts = [x.strip() for x in (mode_tag or "").split("|")]
    return (parts[0] if parts and parts[0] else "#Leech"), (parts[1] if len(parts) > 1 else "")


def _mention(name, uid):
    return f'<a href="tg://user?id={uid}">{_html.escape(str(name))}</a>' if uid else _html.escape(str(name))


def _who(m):
    """(first_name, id) — survives anonymous-admin / channel posts where from_user is None."""
    if m.from_user:
        return (m.from_user.first_name or "User"), m.from_user.id
    if getattr(m, "sender_chat", None):
        return (m.sender_chat.title or "Chat"), m.sender_chat.id
    return "User", 0


def _is_owner(uid):
    return config.OWNER_ID == _OWNER_PLACEHOLDER or uid == config.OWNER_ID


def is_authorized(m):
    """Owner, AUTH_USERS, or any message inside an AUTH_CHATS group.
    Open to everyone only while OWNER_ID is still the placeholder."""
    if config.OWNER_ID == _OWNER_PLACEHOLDER:
        return True
    _, uid = _who(m)
    return uid == config.OWNER_ID or uid in config.AUTH_USERS or m.chat.id in config.AUTH_CHATS


async def _deny(m):
    await reply_message_async(m, "⛔ You are not authorized to use this bot.", parse_mode=None)


# ================= SMALL HELPERS =================

_URL_RE = _re.compile(r"(magnet:\?\S+|https?://\S+)")
CANCEL_MSG = "Upload manually cancelled by user."
ARCHIVE_EXTS = (".7z", ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def _link_from_reply(m):
    r = m.reply_to_message
    if not r:
        return None
    mm = _URL_RE.search((r.text or r.caption or ""))
    return mm.group(1) if mm else None


def _rm_path(p):
    """Delete a file or directory tree, never raise."""
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.lexists(p):
            os.remove(p)
    except Exception:
        pass


async def _rm_paths(paths):
    for p in paths:
        if p:
            await asyncio.to_thread(_rm_path, p)


def _enough_disk(need):
    """True if DOWNLOAD_DIR has room for `need` bytes (+5%)."""
    try:
        return shutil.disk_usage(GLOBAL_STATE["DOWNLOAD_DIR"]).free >= need * 1.05
    except Exception:
        return True


def split_file(path, part_size, chunk=8 * 1024 * 1024):
    """Byte-split `path` into path.001, path.002 ... Returns the part paths."""
    size = os.path.getsize(path)
    parts = []
    with open(path, "rb") as src:
        for i in range(1, (size + part_size - 1) // part_size + 1):
            part = f"{path}.{i:03d}"
            remaining = min(part_size, size - (i - 1) * part_size)
            with open(part, "wb") as dst:
                while remaining > 0:
                    buf = src.read(min(chunk, remaining))
                    if not buf:
                        break
                    dst.write(buf)
                    remaining -= len(buf)
            parts.append(part)
    return parts


# ================= UPLOAD =================

async def _send_with_retry(app, chat_id, path, gid, offset, total, caption, kind="doc", thumb=None, extra=None):
    """send_document with progress, FloodWait handling and 2 retries on network
    errors (10s, 20s backoff). Cancel is honoured immediately."""
    start = time.time()

    def progress(current, _total):
        entry = GLOBAL_STATE["ACTIVE"].get(gid)
        if entry is None or entry.get("cancel"):
            raise Exception(CANCEL_MSG)
        el = time.time() - start
        entry["progress"] = {"done": offset + current, "total": total,
                             "speed": current / el if el > 0 else 0}

    delays = [10, 20]
    for attempt in range(len(delays) + 1):
        try:
            if kind == "video":
                return await app.send_video(
                    chat_id, path, caption=caption, parse_mode=enums.ParseMode.HTML,
                    thumb=thumb, progress=progress, **(extra or {})
                )
            return await app.send_document(
                chat_id, path, caption=caption, parse_mode=enums.ParseMode.HTML,
                thumb=thumb, progress=progress
            )
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError) as e:
            cancelled = GLOBAL_STATE["ACTIVE"].get(gid, {}).get("cancel", True)
            if cancelled or not os.path.exists(path) or attempt >= len(delays):
                raise
            print(f"⚠️ Upload {gid} network error ({e!r}); retry in {delays[attempt]}s")
            await asyncio.sleep(delays[attempt])
    raise RuntimeError("Upload failed after retries")


async def upload_file(app, target_chat, file_path, name, file_size, gid):
    """Upload one file (auto-split above SPLIT_SIZE). Returns (ok, error_text)."""
    async with (UPLOAD_SEMAPHORE or _NoSlot()):
        entry = GLOBAL_STATE["ACTIVE"].get(gid)
        parts = [file_path]
        ok, err = False, ""
        GLOBAL_STATE["UPLOAD_COUNT"] += 1
        try:
            if entry is None or entry.get("cancel"):
                raise Exception(CANCEL_MSG)
            entry["queued"] = False                 # this file is the active one now
            entry["start_time"] = time.time()
            prefs = await us.get(entry.get("user_id", 0))
            tpl = prefs.get("caption") or config.LEECH_CAPTION
            media = None
            _, media = await build_caption(tpl, file_path, name, file_size)   # primes ffprobe once

            limit = config.SPLIT_SIZE
            if limit and file_size > limit:
                entry["status"] = "Splitting"
                parts = await asyncio.to_thread(split_file, file_path, limit)
                await asyncio.to_thread(_rm_path, file_path)   # free disk, parts hold the data
                entry["status"] = "Upload"

            total = sum(os.path.getsize(p) for p in parts)
            offset = 0
            for part in parts:
                psize = os.path.getsize(part)
                pname = name if len(parts) == 1 else os.path.basename(part)
                kind, thumb, extra, auto_thumb = "doc", prefs["thumb"], None, None
                # video mode: only for whole (unsplit) video files — split parts aren't playable
                if prefs["mode"] == "video" and len(parts) == 1 and us.is_video(part):
                    dur, w, h = await us.probe_video(part)
                    if not thumb:
                        auto_thumb = thumb = await us.make_thumb(part, dur)
                    kind = "video"
                    extra = {"duration": int(dur), "width": w, "height": h, "supports_streaming": True}
                try:
                    cap, media = await build_caption(tpl, file_path, pname, psize, media)
                    await _send_with_retry(app, target_chat, part, gid, offset, total,
                                           cap, kind, thumb, extra)
                finally:
                    if auto_thumb:
                        await asyncio.to_thread(_rm_path, auto_thumb)
                offset += psize
            ok = True
        except Exception as e:
            if CANCEL_MSG in str(e) or "cancelled by user" in str(e):
                err = "cancelled"
            else:
                err = str(e) or e.__class__.__name__
                print(f"❌ Upload {gid} failed: {e!r}")
        finally:
            GLOBAL_STATE["UPLOAD_COUNT"] -= 1
            GLOBAL_STATE["ACTIVE"].pop(gid, None)
            for p in set(parts + [file_path]):
                await asyncio.to_thread(_rm_path, p)
    return ok, err


# ================= COMMAND HANDLERS =================

async def start_handler(_, m):
    user_name = m.from_user.first_name if m.from_user else "there"
    await reply_message_async(
        m,
        f"👋 Hello, **{user_name}**!\n"
        "I leech links, torrents and videos to Telegram — **Aria2**, **qBittorrent** and **yt-dlp**.\n\n"
        "Type /help to see every command.",
        parse_mode=enums.ParseMode.MARKDOWN)


async def help_handler(_, m):
    PARSE_MODE = enums.ParseMode.MARKDOWN
    help_message = (
        "**📚 Commands**\n"
        "┠ `/leech` `/l` `<url>` — direct link via Aria2 (`-e` extract, `-z [pass]` zip, `-n <name>` rename)\n"
        "┠ Reply to a **Telegram file** with `/l` to re-upload it (rename/extract/zip/caption rules apply)\n"
        "┠ `/l -i 5` replying to the first file/link — leech it and the next 4 messages\n"
        "┠ `/unzipleech` `/uzl` `<url>` — leech + extract archive\n"
        "┠ `/qbleech` `/ql` `/qb` `<magnet|.torrent>` — torrent via **qBittorrent** (or reply to a `.torrent`)\n"
        "┠ `/qbunzipleech` `/quzl` — qBit leech + extract\n"
        "┠ `/aria` `/al` `<magnet>` — torrent via **Aria2**\n"
        "┠ `/ytdl` `/y` `/ytdlleech` `/yl` `<url>` — **yt-dlp** (quality buttons, `-q 720`, `-np`, `-n name`)\n"
        "┠ `/status` `/s` — live dashboard\n"
        "┠ `/cancel_<gid>` `/c_<gid>` — cancel a task  •  `/cancelall` `/call` — cancel everything (owner)\n"
        "┠ `/usetting` `/us` `/settings` — upload type, thumbnail, dump, name rules\n"
        "┠ `/setthumb` `/delthumb` `/setdump` `/deldump` `/setswap` `/setprefix` `/setsuffix` `/setcaption`\n"
        "┠ `/setcookies` (reply to cookies.txt) `/delcookies` — yt-dlp login cookies (owner: `default`)\n"
        "┠ `/stats` `/st` — system stats  •  `/ping`\n"
        "┠ `/authorize` `/a` `/unauthorize` `/ua` — allow / remove a user or chat (owner)\n"
        "┠ `/log` — bot log (owner)  •  `/restart` `/r` — reboot (owner)\n"
        "┖ `-z` (zip) works on /l /qb /aria /y. No password = `.zip`, with password = AES `.7z`. Reply to a link with any leech command.\n\n"
        "**⚡ Uploads:** each task sends its files one by one, tasks upload in parallel & share speed, auto-retry, files over the Telegram limit are split into `.001 .002 …`\n"
        "**📦 Extract:** `.zip` `.7z` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` (no `.rar`)"
    )
    await reply_message_async(m, help_message, parse_mode=PARSE_MODE)


async def ping_handler(_, m):
    t0 = time.time()
    msg = await reply_message_async(m, "🏓 Pong!", parse_mode=None)
    if msg:
        await edit_message_async(msg, f"🏓 Pong! `{(time.time() - t0) * 1000:.0f} ms`",
                                 parse_mode=enums.ParseMode.MARKDOWN)


# ----- Aria2: /l (direct link) -----

def _tg_tracker(app, src):
    """Download a Telegram file (reply source) into the shared pipeline."""
    async def tracker(entry, gid):
        media, name = _tg_media(src)
        size = getattr(media, "file_size", 0) or 0
        entry["name"] = name
        entry["progress"] = {"done": 0, "total": size, "speed": 0}
        if size and not _enough_disk(size):
            entry["fail_reason"] = f"Not enough disk space for {format_size(size)}"
            return {"ok": False, "err": "no disk space", "name": name}
        folder = os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"tg_{gid}")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, name)
        t0 = time.time()

        def progress(cur, tot):
            if entry.get("cancel"):
                raise Exception("Download cancelled by user.")
            el = time.time() - t0
            entry["progress"] = {"done": cur, "total": tot or size, "speed": cur / el if el > 0 else 0}

        try:
            path = await app.download_media(src, file_name=target, progress=progress)
        except Exception as e:
            await _rm_paths([folder])
            if entry.get("cancel"):
                return {"cancelled": True}
            return {"ok": False, "err": str(e) or e.__class__.__name__, "name": name}
        if entry.get("cancel"):
            await _rm_paths([folder])
            return {"cancelled": True}
        if not path:
            await _rm_paths([folder])
            return {"ok": False, "err": "Telegram returned no file", "name": name}
        return {"ok": True, "gid": gid, "name": name, "paths": [str(path)], "cleanup": [folder]}

    return tracker


async def _leech_one(app, m, url, src, extract_mode, rename_to, zip_opt, new_dash=True):
    """Start ONE leech task from a link (Aria2) or a Telegram message with a file."""
    if src is not None:
        gid = f"tg{src.id}{int(time.time() * 1000) % 10**6:x}"
        return await _run_task(
            app, m, gid, _tg_tracker(app, src), extract_mode,
            mode_tag="#Extract | #Telegram" if extract_mode else "#Leech | #Telegram",
            rename_to=rename_to, engine="Telegram", zip_opt=zip_opt, new_dash=new_dash)
    try:
        options = {
            "dir": GLOBAL_STATE["DOWNLOAD_DIR"],
            "max-connection-per-server": "4",
            "min-split-size": "1M",
            "split": "4",
        }
        dl = await asyncio.to_thread(ARIA2_API.add_uris, [url], options)
    except Exception as e:
        return await reply_message_async(m, f"Failed to start download: {e}", parse_mode=None)
    await _run_task(
        app, m, dl.gid, _aria_tracker(dl), extract_mode,
        mode_tag="#Extract | #Aria2" if extract_mode else "#Leech | #Aria2",
        rename_to=rename_to, engine=f"Aria2 v{GLOBAL_STATE.get('ARIA_VER', '?')}",
        zip_opt=zip_opt, new_dash=new_dash)


MULTI_MAX = 20


async def leech_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)

    args = list(m.command[1:])
    extract_mode = "-e" in args or m.command[0].lower() in ("unzipleech", "uzl")
    args = [a for a in args if a != "-e"]
    args, multi = _parse_multi(args)
    args, zip_opt = _parse_zip(args)
    args, rename_to = _extract_rename_flag(args)
    r = m.reply_to_message

    # ── multi: /l -i N replying to the first message → it and the next N-1 messages ──
    if multi:
        if not r:
            return await reply_message_async(
                m, "Reply to the FIRST file/link with `/l -i <count>` (e.g. `/l -i 5`).",
                parse_mode=enums.ParseMode.MARKDOWN)
        n = min(multi, MULTI_MAX)
        try:
            got = await app.get_messages(m.chat.id, list(range(r.id, r.id + n)))
        except Exception as e:
            return await reply_message_async(m, f"❌ Could not read the messages: {e}", parse_mode=None)
        items = []
        for x in got if isinstance(got, list) else [got]:
            if not x or getattr(x, "empty", False) or x.id == m.id:
                continue
            media, _ = _tg_media(x)
            if media:
                items.append((None, x))
                continue
            mm = _URL_RE.search(x.text or x.caption or "")
            if mm and not mm.group(1).startswith("magnet:"):
                items.append((mm.group(1), None))
        if not items:
            return await reply_message_async(m, "⚠️ No files or links found in those messages.", parse_mode=None)
        await reply_message_async(
            m, f"📥 Starting <b>{len(items)}</b> task(s)" + (f" (limit {MULTI_MAX})" if multi > MULTI_MAX else ""),
            parse_mode=enums.ParseMode.HTML)
        rn = rename_to if len(items) == 1 else None      # -n only makes sense for one file

        async def go(i, url, src):
            await asyncio.sleep(i * 0.7)                 # stagger so Telegram / aria2 are not hammered
            await _leech_one(app, m, url, src, extract_mode, rn, zip_opt, new_dash=(i == 0))

        await asyncio.gather(*[go(i, u, sm) for i, (u, sm) in enumerate(items)])
        return

    # ── single: link in args > file in reply > link in reply ──
    url = args[0] if args else None
    src = None
    if not url:
        media, mname = _tg_media(r)
        if media:
            if mname.lower().endswith(".torrent"):
                return await reply_message_async(m, "That is a .torrent — use `/qb` (or `/aria`) on it.",
                                                 parse_mode=enums.ParseMode.MARKDOWN)
            src = r
        else:
            url = _link_from_reply(m)

    if not url and src is None:
        return await reply_message_async(
            m,
            "Usage:\n"
            "`/l <direct_url>`             — Leech and upload\n"
            "`/l <direct_url> -e`          — Leech, extract, then upload all files\n"
            "`/l <direct_url> -z [pass]`   — Leech, pack into zip (or password .7z), upload\n"
            "`/l <direct_url> -n <name>`   — Leech and rename before upload\n"
            "Reply to a message with a link OR a Telegram file with `/l`.\n"
            "`/l -i 5` replying to the first file/link — leech that message and the next 4.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    await _leech_one(app, m, url, src, extract_mode, rename_to, zip_opt)


# ----- Aria2: /ql (magnet / .torrent) -----

_ARIA_TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce", "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce", "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce", "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce", "udp://open.stealth.si:80/announce",
    "udp://tracker-udp.gbitt.info:80/announce", "udp://tracker.theoks.net:6969/announce",
    "udp://tracker.bittor.pw:1337/announce", "udp://opentracker.i2p.rocks:6969/announce",
    "http://tracker.openbittorrent.com:80/announce", "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.gbitt.info:443/announce", "https://tracker.tamersunion.org:443/announce",
])


async def _download_torrent_doc(app, replied):
    """If `replied` is a .torrent document, download it and return its local path."""
    if not (replied and replied.document):
        return None
    fname = (replied.document.file_name or "").lower()
    if not (fname.endswith(".torrent") or replied.document.mime_type == "application/x-bittorrent"):
        return None
    target = os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"tor_{replied.document.file_unique_id}.torrent")
    os.makedirs(os.path.dirname(target), exist_ok=True)   # dir may be wiped by a cleanup
    last = None
    for _ in range(3):
        try:
            return await app.download_media(replied, file_name=target)
        except Exception as e:
            last = e
            os.makedirs(os.path.dirname(target), exist_ok=True)
            await asyncio.sleep(2)
    raise last


async def ql_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)

    args = list(m.command[1:])
    extract_mode = "-e" in args
    args = [a for a in args if a != "-e"]
    args, zip_opt = _parse_zip(args)
    args, rename_to = _extract_rename_flag(args)
    magnet_uri = args[0] if args else _link_from_reply(m)

    torrent_path = None
    try:
        torrent_path = await _download_torrent_doc(app, m.reply_to_message)
    except Exception as e:
        return await reply_message_async(m, f"❌ Failed to fetch .torrent file: {e}", parse_mode=None)

    if not torrent_path and not magnet_uri:
        return await reply_message_async(
            m,
            "Usage:\n"
            "`/aria <magnet_link>`          — Leech a magnet link (Aria2)\n"
            "`/aria <magnet_link> -e`       — Leech magnet, extract, then upload\n"
            "`/aria <magnet_link> -n <name>` — Leech magnet and rename before upload\n"
            "Reply to a `.torrent` file with `/aria` to leech it\n"
            "Want qBittorrent instead? Use `/ql`.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    if not torrent_path and not magnet_uri.startswith("magnet:"):
        return await reply_message_async(m, "❌ Not a valid magnet link (must start with `magnet:`).",
                                         parse_mode=enums.ParseMode.MARKDOWN)

    options = {
        "dir": GLOBAL_STATE["DOWNLOAD_DIR"],
        "max-connection-per-server": "4",
        "seed-time": "0",
        "bt-stop-timeout": "600",
        "bt-tracker": _ARIA_TRACKERS,
        "bt-tracker-connect-timeout": "10",
        "bt-tracker-timeout": "10",
        "enable-dht": "true",
        "enable-peer-exchange": "true",
        "bt-max-peers": "200",
        "bt-request-peer-speed-limit": "50M",
    }
    try:
        if torrent_path:
            dl = await asyncio.to_thread(ARIA2_API.add_torrent, torrent_path, options=options)
        else:
            dl = await asyncio.to_thread(ARIA2_API.add_magnet, magnet_uri, options=options)
    except Exception as e:
        if torrent_path:
            await asyncio.to_thread(_rm_path, torrent_path)
        return await reply_message_async(m, f"Failed to start torrent/magnet download: {e}", parse_mode=None)

    await _run_task(
        app, m, dl.gid, _aria_tracker(dl, extra_cleanup=[torrent_path] if torrent_path else []),
        extract_mode,
        mode_tag="#Extract | #Torrent" if extract_mode else "#Leech | #Magnet",
        rename_to=rename_to, engine=f"Aria2 v{GLOBAL_STATE.get('ARIA_VER', '?')}", zip_opt=zip_opt
    )


# ----- qBittorrent: /qb -----

async def qb_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    if not qb.QBIT_AVAILABLE:
        return await reply_message_async(m, "❌ `aioqbt` is not installed (`pip install aioqbt`).",
                                         parse_mode=enums.ParseMode.MARKDOWN)

    args = list(m.command[1:])
    extract_mode = "-e" in args or m.command[0].lower() in ("qbunzipleech", "quzl")
    args = [a for a in args if a != "-e"]
    args, zip_opt = _parse_zip(args)
    args, rename_to = _extract_rename_flag(args)
    source = args[0] if args else _link_from_reply(m)

    torrent_bytes = None
    try:
        tpath = await _download_torrent_doc(app, m.reply_to_message)
        if tpath:
            torrent_bytes = await asyncio.to_thread(Path(tpath).read_bytes)
            await asyncio.to_thread(_rm_path, tpath)
    except Exception as e:
        return await reply_message_async(m, f"❌ Failed to fetch .torrent file: {e}", parse_mode=None)

    if torrent_bytes is None and not source:
        return await reply_message_async(
            m,
            "Usage (qBittorrent engine):\n"
            "`/qb <magnet>`             — Leech a magnet\n"
            "`/qb <.torrent URL>`       — Leech from a torrent link\n"
            "`/qb <magnet> -e`          — Leech, extract, then upload\n"
            "`/qb <magnet> -n <name>`   — Leech and rename (single-file torrents)\n"
            "Reply to a `.torrent` file (or a message with a link) with `/qb`.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    if torrent_bytes is None and not (source.startswith("magnet:") or source.startswith(("http://", "https://"))):
        return await reply_message_async(m, "❌ Give a `magnet:` link, a `.torrent` URL, or reply to a `.torrent` file.",
                                         parse_mode=enums.ParseMode.MARKDOWN)

    if torrent_bytes is not None and qb.torrent_hash(torrent_bytes) is None:
        return await reply_message_async(m, "❌ That file is not a valid .torrent.", parse_mode=None)

    tag = f"ldm{int(time.time() * 1000) % 10**10}"
    try:
        h = await qb.add(torrent_bytes if torrent_bytes is not None else source, tag)
    except Exception as e:
        return await reply_message_async(
            m, f"❌ qBittorrent add failed: {e}\n(Is qbittorrent-nox running on port {config.QBIT_PORT}?)",
            parse_mode=None)

    gid = f"qb{h[:12]}"
    if gid in GLOBAL_STATE["ACTIVE"]:
        await qb.remove(h, False)   # same torrent already running here; don't double track
        return await reply_message_async(m, "⚠️ That torrent is already being leeched.", parse_mode=None)

    await _run_task(
        app, m, gid, _qbit_tracker(h), extract_mode,
        mode_tag="#Extract | #qBit" if extract_mode else "#Leech | #qBit",
        rename_to=rename_to, engine=f"qBittorrent v{qb.version()}", extra_state={"engine_id": "qbit", "hash": h},
        zip_opt=zip_opt
    )


# ================= TASK PIPELINE =================
# Every engine supplies a *tracker* coroutine: it polls its engine, keeps the
# dashboard entry fresh, and returns a result dict:
#   {"ok": True,  "gid": str, "name": str, "paths": [files], "cleanup": [paths]}
#   {"ok": False, "err": "...", "name": str}      failure
#   {"cancelled": True}                            user cancelled (tracker already cleaned up)
# The shared tail (_after_download) renames / extracts / uploads / cleans up.

def _rekey(old, new):
    if old != new and old in GLOBAL_STATE["ACTIVE"]:
        GLOBAL_STATE["ACTIVE"][new] = GLOBAL_STATE["ACTIVE"].pop(old)


def _aria_tracker(dl, extra_cleanup=()):
    async def tracker(entry, gid):
        nonlocal dl
        fails = 0
        disk_checked = False
        while True:
            if entry["cancel"]:
                try:
                    await asyncio.to_thread(dl.update)
                    paths = [str(f.path) for f in dl.files if f.path]
                except Exception:
                    paths = []
                try:
                    await asyncio.to_thread(ARIA2_API.remove, [dl], force=True, files=True)
                except Exception as e:
                    print(f"Aria2 remove failed for {gid}: {e}")
                await _rm_paths(paths + [p + ".aria2" for p in paths] + list(extra_cleanup))
                return {"cancelled": True}

            try:
                await asyncio.to_thread(dl.update)
                fails = 0
            except Exception as e:
                fails += 1
                if fails >= 5:
                    return {"ok": False, "err": f"aria2 RPC error: {e}", "name": entry.get("name", "?")}
                await asyncio.sleep(2)
                continue

            # Magnet metadata finished → aria2 spawns a NEW gid for the real download.
            followed = dl.followed_by_ids
            if dl.is_complete and followed:
                new_gid = followed[0]
                dl = await asyncio.to_thread(ARIA2_API.get_download, new_gid)
                _rekey(gid, new_gid)
                gid = new_gid
                continue

            if dl.has_failed:
                return {"ok": False, "err": dl.error_message or "aria2 error", "name": dl.name}
            if dl.is_removed:
                return {"ok": False, "err": "download was removed from aria2", "name": dl.name}
            if dl.is_complete:
                break

            entry["name"] = dl.name
            entry["progress"] = {"done": dl.completed_length, "total": dl.total_length, "speed": dl.download_speed}
            if dl.bittorrent and dl.connections is not None:
                entry["extra"] = f"<b>Seeders :</b> {dl.num_seeders} | <b>Conn :</b> {dl.connections}"
            if not disk_checked and dl.total_length > 0:
                disk_checked = True
                if not _enough_disk(dl.total_length - dl.completed_length):
                    entry["cancel"] = True
                    entry["fail_reason"] = f"Not enough disk space for {format_size(dl.total_length)}"
            await asyncio.sleep(2)

        # complete
        files = [f for f in dl.files if f.selected and f.path and "[METADATA]" not in str(f.path)]
        paths = [str(f.path) for f in files]
        cleanup = [p + ".aria2" for p in paths] + list(extra_cleanup)
        root = os.path.join(str(dl.dir), dl.name)
        if len(paths) > 1 and os.path.isdir(root):
            cleanup.append(root)           # multi-file torrent folder
        return {"ok": True, "gid": gid, "name": dl.name, "paths": paths, "cleanup": cleanup}

    return tracker


def _qbit_tracker(h):
    async def tracker(entry, gid):
        fails = 0
        meta_since = None
        disk_checked = False
        while True:
            if entry["cancel"]:
                try:
                    await qb.remove(h, True)
                except Exception as e:
                    print(f"qBit remove failed for {h}: {e}")
                return {"cancelled": True}

            try:
                t = await qb.info(h)
                fails = 0 if t else fails + 1
            except Exception as e:
                t = None
                fails += 1
                print(f"qBit poll error: {e}")
            if t is None:
                if fails >= 5:
                    return {"ok": False, "err": "torrent vanished from qBittorrent", "name": entry.get("name", "?")}
                await asyncio.sleep(2)
                continue

            state = str(getattr(t.state, "value", t.state))
            entry["name"] = t.name
            entry["status"] = qb.state_text(t.state)
            entry["progress"] = {"done": int(t.size * t.progress), "total": t.size, "speed": t.dlspeed}
            entry["extra"] = f"<b>Seeders :</b> {t.num_seeds} | <b>Leechers :</b> {t.num_leechs}"

            if qb.is_error(t):
                try:
                    await qb.remove(h, True)
                except Exception:
                    pass
                return {"ok": False, "err": f"qBittorrent state: {state}", "name": t.name}

            if qb.is_done(t):
                break

            # magnet that never resolves metadata → give up after 10 min (same as aria2 bt-stop-timeout)
            if state == "metaDL":
                meta_since = meta_since or time.time()
                if time.time() - meta_since > 600:
                    try:
                        await qb.remove(h, True)
                    except Exception:
                        pass
                    return {"ok": False, "err": "no metadata after 10 min (dead magnet?)", "name": t.name}
            else:
                meta_since = None

            if not disk_checked and t.size > 0:
                disk_checked = True
                if not _enough_disk(t.size - int(t.size * t.progress)):
                    entry["cancel"] = True
                    entry["fail_reason"] = f"Not enough disk space for {format_size(t.size)}"
            await asyncio.sleep(2)

        # complete → grab the file list, then drop the torrent from qBit (stops seeding,
        # frees RAM) but KEEP the files; we own them from here.
        fl = await qb.files(h)
        save_path = str(t.save_path)
        paths = [os.path.join(save_path, f.name) for f in fl if int(f.priority) != 0]
        cleanup = []
        if fl and "/" in fl[0].name:
            cleanup.append(os.path.join(save_path, fl[0].name.split("/")[0]))
        try:
            await qb.remove(h, False)
        except Exception as e:
            print(f"qBit remove (keep files) failed: {e}")
        return {"ok": True, "gid": gid, "name": t.name, "paths": paths, "cleanup": cleanup}

    return tracker


async def _run_task(app, m, gid, tracker, extract_mode, mode_tag, rename_to, engine, extra_state=None,
                    zip_opt=None, new_dash=True):
    GLOBAL_STATE["DOWNLOAD_COUNTER"] += 1
    start_time = time.time()
    first, uid = _who(m)

    await _ensure_dashboard(app, m.chat.id, force_new=new_dash)

    entry = {
        "cancel": False, "kind": "⬇️ DL", "name": "Fetching info...", "status": "Download",
        "engine": engine, "progress": {"done": 0, "total": 0, "speed": 0},
        "start_time": start_time, "chat_id": m.chat.id,
        "user_first": first, "user_id": uid, "mode_tag": mode_tag,
    }
    entry.update(extra_state or {})
    GLOBAL_STATE["ACTIVE"][gid] = entry
    GLOBAL_STATE["DOWNLOAD_COUNT"] += 1

    res = {"ok": False, "err": "unknown error", "name": "?"}
    try:
        res = await tracker(entry, gid)
    except Exception as e:
        print(f"❌ tracker crashed: {e!r}")
        res = {"ok": False, "err": f"internal error: {e}", "name": entry.get("name", "?")}
    finally:
        GLOBAL_STATE["DOWNLOAD_COUNT"] -= 1
        # tracker may have re-keyed the entry (aria2 magnet handoff)
        for k in [k for k, v in GLOBAL_STATE["ACTIVE"].items() if v is entry]:
            GLOBAL_STATE["ACTIVE"].pop(k, None)

    if res.get("cancelled"):
        reason = entry.get("fail_reason")
        text = f"🛑 {reason}" if reason else "🛑 Download cancelled."
        return await reply_message_async(m, text, parse_mode=None)

    if not res.get("ok"):
        reason = entry.get("fail_reason") or res.get("err", "unknown")
        return await reply_message_async(
            m, f"❌ Download <b>{_html.escape(str(res.get('name', '?')))}</b> failed.\n"
               f"Reason: {_html.escape(str(reason))}",
            parse_mode=enums.ParseMode.HTML
        )

    try:
        await _after_download(app, m, res["gid"], res["paths"], res["cleanup"], extract_mode,
                              mode_tag, rename_to, res["name"], start_time, zip_opt)
    except Exception as e:
        print(f"❌ post-download crashed: {e!r}")
        await _rm_paths(res.get("cleanup", []) + res.get("paths", []))
        await reply_message_async(m, f"❌ Post-processing failed: {e}", parse_mode=None)


def _pack_archive(paths, out_path, password, entry):
    """Blocking. password None-safe: '' -> plain .zip (stored, fast), 'x' -> AES-256 .7z.
    Updates entry['progress']; raises nothing (returns (ok, error))."""
    import zipfile
    sizes = []
    for p in paths:
        try:
            sizes.append(os.path.getsize(p))
        except OSError:
            sizes.append(0)
    total = max(sum(sizes), 1)
    try:
        base = os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:
        base = os.path.dirname(paths[0])
    names = [os.path.relpath(p, base).replace(os.sep, "/") for p in paths]
    t0 = time.time()
    done = 0

    def tick(n):
        nonlocal done
        done += n
        if entry.get("cancel"):
            raise Exception("Archive cancelled by user.")
        el = time.time() - t0
        entry["progress"] = {"done": done, "total": total, "speed": done / el if el > 0 else 0}

    try:
        if password:
            import py7zr
            filters = [{"id": py7zr.FILTER_COPY}, {"id": py7zr.FILTER_CRYPTO_AES256_SHA256}]
            with py7zr.SevenZipFile(out_path, "w", password=password, filters=filters) as arc:
                for p, nm, sz in zip(paths, names, sizes):
                    arc.write(p, nm)
                    tick(sz)
        else:
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for p, nm in zip(paths, names):
                    zi = zipfile.ZipInfo.from_file(p, nm)
                    zi.compress_type = zipfile.ZIP_STORED
                    with open(p, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                        while True:
                            chunk = src.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                            tick(len(chunk))
        return True, ""
    except Exception as e:
        _rm_path(out_path)
        return False, str(e) or e.__class__.__name__


async def _after_download(app, m, gid, paths, cleanup, extract_mode, mode_tag, rename_to, dl_name, start_time,
                          zip_opt=None):
    first, uid = _who(m)
    target_chat = (await us.get(uid))["dump"] or config.LEECH_DUMP or m.chat.id
    cleanup = list(cleanup)

    # ── 1. validate downloaded files ───────────────────────────────────────
    files = []
    for p in paths:
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                files.append(p)
        except OSError:
            pass
    if not files:
        await _rm_paths(cleanup + paths)
        return await reply_message_async(m, "❌ Download complete but no usable files (missing or empty).",
                                         parse_mode=None)

    # ── 2. rename: -n wins for a single file (no rules, like Leechv2); else site-tag
    #       cleanup + Leechv2 name-swap / prefix / suffix rules ──────────────────
    prefs = await us.get(uid)
    renamed = []
    for p in files:
        original = os.path.basename(p)
        if rename_to and len(files) == 1 and zip_opt is None:
            _, orig_ext = os.path.splitext(original)
            _, new_ext = os.path.splitext(rename_to)
            new_name = rename_to if new_ext else f"{rename_to}{orig_ext}"
        else:
            new_name = clean_filename(original)
            if has_rules(prefs):
                new_name = format_filename(new_name, prefs)
        new_name = new_name.replace("/", "_")
        if new_name != original:
            dest = os.path.join(os.path.dirname(p), new_name)
            if not os.path.exists(dest):
                try:
                    os.rename(p, dest)
                    p = dest
                except OSError:
                    pass
        renamed.append(p)
    files = renamed

    # ── 3. extract (-e): archives are unpacked, other files pass through ────
    jobs = []
    fresh = set()   # files that came out of an archive -> still need the name rules
    if extract_mode:
        for i, p in enumerate(files, 1):
            if not p.lower().endswith(ARCHIVE_EXTS):
                jobs.append(p)
                continue
            name = os.path.basename(p)
            extract_dir = os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"extracted_{gid}_{i}")
            os.makedirs(extract_dir, exist_ok=True)
            cleanup.append(extract_dir)
            ex_key = f"{gid}_x{i}"
            GLOBAL_STATE["ACTIVE"][ex_key] = {
                "cancel": False, "chat_id": m.chat.id, "name": name, "kind": "🗜️ EX",
                "progress": {"done": 0, "total": 1, "speed": 0}, "start_time": time.time(),
                "user_first": first, "user_id": uid, "mode_tag": mode_tag,
                "status": "Extract", "engine": "pExtract v2",
            }
            await _ensure_dashboard(app, m.chat.id)
            ok, msg_text = await extract_file(
                p, extract_dir, None, name, 0, first, uid, asyncio.get_running_loop(), progress_gid=ex_key
            )
            was_cancelled = GLOBAL_STATE["ACTIVE"].get(ex_key, {}).get("cancel", False)
            GLOBAL_STATE["ACTIVE"].pop(ex_key, None)
            await asyncio.to_thread(_rm_path, p)       # archive no longer needed
            if not ok:
                await asyncio.to_thread(_rm_path, extract_dir)
                if not was_cancelled:
                    await reply_message_async(
                        m, f"❌ <b>Extraction failed</b> for <code>{_html.escape(name)}</code>:\n"
                           f"<code>{_html.escape(msg_text)}</code>", parse_mode=enums.ParseMode.HTML)
                continue
            for root, _, fnames in os.walk(extract_dir):
                for fname in sorted(fnames):
                    jobs.append(os.path.join(root, fname))
                    fresh.add(jobs[-1])
    else:
        jobs = files

    # ── 3b. zip (-z [password]): pack everything into ONE archive ───────────
    if zip_opt is not None and jobs:
        stem = os.path.splitext(rename_to)[0] if rename_to else (
            os.path.splitext(dl_name)[0] if dl_name and dl_name != "?" else os.path.splitext(os.path.basename(jobs[0]))[0])
        stem = (stem or "archive").replace("/", "_")
        zdir = os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"zip_{gid}")
        os.makedirs(zdir, exist_ok=True)
        cleanup.append(zdir)
        out_path = os.path.join(zdir, stem + (".7z" if zip_opt else ".zip"))
        z_key = f"{gid}_z"
        GLOBAL_STATE["ACTIVE"][z_key] = {
            "cancel": False, "chat_id": m.chat.id, "name": os.path.basename(out_path), "kind": "🗜️ ZIP",
            "progress": {"done": 0, "total": 1, "speed": 0}, "start_time": time.time(),
            "user_first": first, "user_id": uid, "mode_tag": mode_tag,
            "status": "Archive", "engine": "py7zr (AES)" if zip_opt else "zipfile",
        }
        await _ensure_dashboard(app, m.chat.id)
        ok, zerr = await asyncio.to_thread(_pack_archive, jobs, out_path, zip_opt, GLOBAL_STATE["ACTIVE"][z_key])
        was_cancelled = GLOBAL_STATE["ACTIVE"].get(z_key, {}).get("cancel", False)
        GLOBAL_STATE["ACTIVE"].pop(z_key, None)
        if not ok:
            await _rm_paths(cleanup + jobs)
            if not was_cancelled:
                await reply_message_async(m, f"❌ <b>Archive failed:</b> <code>{_html.escape(zerr)}</code>",
                                          parse_mode=enums.ParseMode.HTML)
            return
        await _rm_paths(jobs)                          # sources no longer needed, frees disk
        jobs = [out_path]
        fresh = set() if rename_to else {out_path}

    if not jobs:
        await _rm_paths(cleanup)
        return await reply_message_async(m, "⚠️ Nothing to upload (extraction produced no files).", parse_mode=None)

    # ── 4. queue uploads; semaphore caps real concurrency ──────────────────
    await _ensure_dashboard(app, m.chat.id)
    upload_jobs = []
    for idx, p in enumerate(jobs, 1):
        original = os.path.basename(p)
        name = original
        if p in fresh:
            name = clean_filename(original)
            if has_rules(prefs):
                name = format_filename(name, prefs)
        if name != original:
            dest = os.path.join(os.path.dirname(p), name)
            if not os.path.exists(dest):
                try:
                    os.rename(p, dest)
                    p = dest
                except OSError:
                    name = original
            else:
                name = original
        size = os.path.getsize(p)
        sub = f"{gid}_u{idx}"
        GLOBAL_STATE["ACTIVE"][sub] = {
            "cancel": False, "file_path": p, "name": name, "kind": "⬆️ UL",
            "queued": idx > 1, "file_no": idx, "file_count": len(jobs),
            "progress": {"done": 0, "total": size, "speed": 0},
            "base_gid": gid, "cleanup": cleanup,
            "chat_id": m.chat.id, "start_time": time.time(),
            "user_first": first, "user_id": uid, "mode_tag": mode_tag,
            "status": "Upload", "engine": GLOBAL_STATE.get("ENGINE_NAME", "Pyrogram"),
        }
        upload_jobs.append((p, name, size, sub))

    # one file at a time per task (like Leechv2); other tasks upload in parallel with this one
    results = []
    for p, name, size, sub in upload_jobs:
        results.append(await upload_file(app, target_chat, p, name, size, sub))

    # ── 5. cleanup + completion report ─────────────────────────────────────
    await _rm_paths(cleanup)

    ok_count = sum(1 for ok, _ in results if ok)
    cancelled = any(err == "cancelled" for ok, err in results if not ok)
    if cancelled:
        return   # cancel handler already told the user

    total_size = sum(j[2] for j in upload_jobs)
    elapsed = time_fmt(time.time() - start_time)
    out_mode, in_mode = _split_mode(mode_tag)
    lines = [
        f"<blockquote><b><i>{_html.escape(dl_name)}</i></b></blockquote>",
        "",
        f"┌ <b>Task Size:</b> {format_size(total_size)}",
        f"├ <b>Time Taken:</b> {elapsed}",
        f"├ <b>In Mode:</b> {_html.escape(in_mode)}",
        f"├ <b>Out Mode:</b> {_html.escape(out_mode)}",
        f"├ <b>Total Files:</b> {ok_count}" + ("" if ok_count == len(results) else f"/{len(results)}"),
        f"└ <b>Task By:</b> {_mention(first, uid)}",
    ]
    if ok_count != len(results):
        lines.insert(0, "⚠️ <b>Finished with errors</b>")
    fails = [(job[1], err) for job, (ok, err) in zip(upload_jobs, results) if not ok]
    if fails:
        lines.append("")
        lines.append("<b>Failed:</b>")
        for fname, err in fails[:8]:
            lines.append(f"• <code>{_html.escape(fname)}</code> — {_html.escape(err[:120])}")
    await reply_message_async(m, "\n".join(lines), parse_mode=enums.ParseMode.HTML)


# ================= CANCEL =================

async def _cancel_task(task_id, uid):
    """Cancel one task by id. Returns the reply text."""
    info = GLOBAL_STATE["ACTIVE"].get(task_id)
    if not info:
        return f"Task <b>{_html.escape(task_id)}</b> not found or already complete."
    if not (_is_owner(uid) or info.get("user_id") == uid):
        return "⛔ That task belongs to someone else."

    info["cancel"] = True
    base = info.get("base_gid")

    if "file_path" in info:   # ── upload (one of possibly many siblings) ──
        sibs = [k for k, v in list(GLOBAL_STATE["ACTIVE"].items()) if base and v.get("base_gid") == base] or [task_id]
        doomed = list(info.get("cleanup") or [])
        for k in sibs:
            v = GLOBAL_STATE["ACTIVE"].get(k)
            if v:
                v["cancel"] = True
                doomed.append(v.get("file_path"))
        await _rm_paths(doomed)
        return f"🛑 Cancelled upload <b>{_html.escape(info['name'])}</b>.\nFiles deleted."

    if info.get("kind") == "🗜️ EX":
        return f"🛑 Cancelling extraction of <b>{_html.escape(info['name'])}</b>…"

    if info.get("kind") == "🗜️ ZIP":
        return f"🛑 Cancelling archive <b>{_html.escape(info['name'])}</b>…"

    # ── download: the engine tracker sees the flag and cleans up itself ──
    return f"🛑 Cancelling download <b>{_html.escape(str(info.get('name', task_id)))}</b>…"


async def cancel_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    token = ((m.text or "").split() or [""])[0].split("@")[0]       # drop args and @botname
    mt = _re.match(r"^/(?:cancel|c)\d*_(\S+)", token)               # /c_gid  /cancel_gid  /cancel3_gid
    if not mt:
        return
    task_id = mt.group(1)
    _, uid = _who(m)
    await reply_message_async(m, await _cancel_task(task_id, uid), parse_mode=enums.ParseMode.HTML)


async def cancelall_handler(_, m: Message):
    _, uid = _who(m)
    if not _is_owner(uid):
        return await reply_message_async(m, "⛔ Owner only.", parse_mode=None)
    ids = list(GLOBAL_STATE["ACTIVE"].keys())
    done = 0
    for tid in ids:
        if tid in GLOBAL_STATE["ACTIVE"] and "file_path" not in GLOBAL_STATE["ACTIVE"][tid]:
            await _cancel_task(tid, uid)
            done += 1
    # uploads last (their cancel also wipes sibling files)
    for tid in ids:
        if tid in GLOBAL_STATE["ACTIVE"] and "file_path" in GLOBAL_STATE["ACTIVE"][tid]:
            await _cancel_task(tid, uid)
            done += 1
    await reply_message_async(m, f"🛑 Cancel signal sent to {done} task(s).", parse_mode=None)


async def status_handler(app, m: Message):
    """/status spawns a fresh auto-refreshing dashboard message."""
    if not is_authorized(m):
        return await _deny(m)
    await _ensure_dashboard(app, m.chat.id, force_new=True)


async def status_callback_handler(app, cq: CallbackQuery):
    await cq.answer()


# ================= DASHBOARD (WZML-X style) =================
# One single message per chat, auto-refreshing, paginated. Stops refreshing by
# itself once nothing is active (no endless edits / `top` spawns while idle).

DASH_PAGE_SIZE        = 4   # Leechv2 STATUS_LIMIT (4 keeps the message under Telegram's 4096 chars)
DASH_REFRESH_INTERVAL = 5   # seconds
DASH_IDLE_STOP        = 3   # empty refreshes in a row before the loop parks itself


def leech_bar(pct):
    """Leechv2 bar: [⬢⬢⬢⬡⬡...] 12 cells, one cell per 8%."""
    p = min(max(float(pct or 0), 0), 100)
    full = int(p // 8)
    return "[" + "⬢" * full + "⬡" * (12 - full) + "]"


def _dashboard_tasks(chat_id):
    """Snapshot of all active DL / EX / UL tasks for this chat (insertion order)."""
    tasks = []
    for gid, info in list(GLOBAL_STATE["ACTIVE"].items()):
        if info.get("chat_id") != chat_id or "kind" not in info or info.get("queued"):
            continue
        prog = info.get("progress") or {}
        kind = info["kind"]
        default_status = {"⬇️ DL": "Download", "⬆️ UL": "Upload"}.get(kind, "Extract")
        out_mode, in_mode = _split_mode(info.get("mode_tag", ""))
        tasks.append({
            "gid": gid, "kind": kind,
            "name": info.get("name", "?") + (f"  [{info['file_no']}/{info['file_count']}]" if info.get("file_count", 1) > 1 else ""),
            "done": prog.get("done", 0), "total": prog.get("total", 0), "speed": prog.get("speed", 0),
            "status": info.get("status", default_status),
            "engine": info.get("engine", "?"),
            "in_mode": in_mode, "out_mode": out_mode,
            "extra": info.get("extra", ""),
            "start_time": info.get("start_time", time.time()),
            "user_first": info.get("user_first", "?"),
            "user_id": info.get("user_id", 0),
        })
    return tasks


def _sys_snapshot():
    snap = {"cpu": 0.0, "ram": 0.0, "free": 0, "total": 1, "net_in": 0, "net_out": 0}
    try:
        import psutil
        snap["cpu"] = psutil.cpu_percent(interval=None)
        snap["ram"] = psutil.virtual_memory().percent
        n = psutil.net_io_counters()
        snap["net_in"], snap["net_out"] = n.bytes_recv, n.bytes_sent
    except Exception:
        pass
    try:
        du = shutil.disk_usage(GLOBAL_STATE["DOWNLOAD_DIR"])
        snap["free"], snap["total"] = du.free, du.total
    except Exception:
        pass
    return snap


async def _get_system_stats(app):
    """(cpu%, ram%, free_gb, free_pct, uptime) as display strings."""
    s = await asyncio.to_thread(_sys_snapshot)
    return (f"{s['cpu']:.1f}", f"{s['ram']:.1f}", f"{s['free'] / 1024**3:.1f}",
            f"{s['free'] * 100 / max(s['total'], 1):.0f}", time_fmt(time.time() - BOT_START))


async def render_dashboard(app, chat_id, page):
    """Leechv2 (NEO-WZML) status layout: ⬢⬡ bar, Process/Processed/Status/Speed/Time/Engine/
    In-Out Mode/User/ID/Stop tree, footer with CPU/RAM/FREE, IN/OUT, DL/UL/UP, ☰ ♻️ buttons."""
    tasks = _dashboard_tasks(chat_id)
    n = len(tasks)
    pages = max(1, (n + DASH_PAGE_SIZE - 1) // DASH_PAGE_SIZE)
    page = (page - 1) % pages + 1 if page > pages else (pages - (abs(page) % pages) if page < 1 else page)
    start = (page - 1) * DASH_PAGE_SIZE

    snap = await asyncio.to_thread(_sys_snapshot)
    uptime = time_fmt(time.time() - BOT_START)
    dl_speed = sum(t["speed"] for t in tasks if t["kind"] == "⬇️ DL")
    ul_speed = sum(t["speed"] for t in tasks if t["kind"] == "⬆️ UL")

    if not tasks:
        msg = (
            "<i>No Active Downloads!</i>\n\n"
            "<blockquote><b><i>Bot Stats</i></b></blockquote>\n"
            f" • <b>CPU:</b> {snap['cpu']:.1f}% | <b>F:</b> {format_size(snap['free'])} "
            f"[{snap['free'] * 100 / max(snap['total'], 1):.0f}%]\n"
            f" • <b>RAM:</b> {snap['ram']:.1f}% | <b>UPTIME:</b> {uptime}"
        )
    else:
        msg = ""
        for idx, t in enumerate(tasks[start:start + DASH_PAGE_SIZE], start=start + 1):
            pct = (t["done"] / t["total"] * 100) if t["total"] else 0
            elapsed = time.time() - t["start_time"]
            if t["speed"] > 0 and t["total"] > t["done"]:
                eta = (t["total"] - t["done"]) / t["speed"]
                eta_str, total_str = time_fmt(eta), time_fmt(elapsed + eta)
            else:
                eta_str = total_str = "~"
            rows = [
                f"├ {leech_bar(pct)}",
                f"├ <b>Process :</b> {pct:.2f}%",
                f"├ <b>Processed :</b> {format_size(t['done'])} of {format_size(t['total'])}",
                f"├ <b>Status :</b> <i>{_html.escape(t['status'])}...</i>⬘",
                f"├ <b>Speed :</b> {format_speed(t['speed'])}",
                f"├ <b>Time :</b> {time_fmt(elapsed)} of {total_str} ( {eta_str} )",
                f"├ <b>Engine :</b> {_html.escape(str(t['engine']))}",
                f"├ <b>In Mode :</b> {_html.escape(t['in_mode'])}",
                f"├ <b>Out Mode :</b> {_html.escape(t['out_mode'])}",
            ]
            if t["extra"]:
                rows.append(f"├ {t['extra']}")
            rows += [
                f"├ <b>User :</b> {_mention(t['user_first'], t['user_id'])}",
                f"├ <b>ID :</b> {t['user_id']}",
                f"└ <b>Stop :</b> /c_{t['gid']}",
            ]
            rows[0] = "┌" + rows[0][1:]
            msg += f"{idx}. {_html.escape(str(t['name'])[:80])}\n" + "\n".join(rows) + "\n\n"

        msg += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        if n > DASH_PAGE_SIZE:
            msg += f"<b>TASKS:</b> {n}\n"
        msg += (
            f"<b>CPU:</b> {snap['cpu']:.1f}% | <b>RAM:</b> {snap['ram']:.1f}% | "
            f"<b>FREE:</b> {format_size(snap['free'])}\n"
            f"<b>IN:</b> {format_size(snap['net_in'])} | <b>OUT:</b> {format_size(snap['net_out'])}\n"
            f"<b>DL:</b> {format_size(dl_speed)}/s | <b>UL:</b> {format_size(ul_speed)}/s | "
            f"<b>UP:</b> {uptime}"
        )

    if pages > 1:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("☰", callback_data="dash_ov")],
            [InlineKeyboardButton("❮❮", callback_data="dash_prev"),
             InlineKeyboardButton(f"♻️ {page}/{pages}", callback_data="dash_refresh"),
             InlineKeyboardButton("❯❯", callback_data="dash_next")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("☰", callback_data="dash_ov"),
            InlineKeyboardButton("♻️", callback_data="dash_refresh"),
        ]])
    return msg, keyboard, page


async def _ensure_dashboard(app, chat_id, force_new=False):
    """
    Auto-opens the dashboard for this chat.
    - force_new=False: reuse the running dashboard if there is one.
    - force_new=True (new /l, /ql, /qb or /status): freeze the old message in place
      (buttons stripped, never deleted) and pop a fresh one.
    """
    existing = GLOBAL_STATE["DASHBOARDS"].get(chat_id)

    if existing and existing.get("running"):
        if not force_new:
            return
        existing["running"] = False
        try:
            await existing["msg"].edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    text, keyboard, page = await render_dashboard(app, chat_id, 1)
    msg = await app.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
    gen = time.time()
    GLOBAL_STATE["DASHBOARDS"][chat_id] = {"msg": msg, "page": 1, "running": True, "gen": gen}
    asyncio.create_task(_dashboard_loop(app, chat_id, gen))


async def _dashboard_loop(app, chat_id, gen):
    """Auto-refresh; exits when superseded (gen mismatch) or after DASH_IDLE_STOP empty refreshes."""
    idle = 0
    while True:
        entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
        if not entry or not entry.get("running") or entry.get("gen") != gen:
            return
        text, keyboard, page = await render_dashboard(app, chat_id, entry["page"])
        entry["page"] = page
        idle = idle + 1 if not _dashboard_tasks(chat_id) else 0
        if idle >= DASH_IDLE_STOP:
            entry["running"] = False
            await edit_message_async(entry["msg"], text, parse_mode=enums.ParseMode.HTML, reply_markup=None)
            return
        await edit_message_async(entry["msg"], text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        await asyncio.sleep(DASH_REFRESH_INTERVAL)


async def dashboard_callback_handler(app, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    uid = cq.from_user.id if cq.from_user else 0
    if config.OWNER_ID != _OWNER_PLACEHOLDER and not (
            uid == config.OWNER_ID or uid in config.AUTH_USERS or chat_id in config.AUTH_CHATS):
        return await cq.answer("Not authorized.", show_alert=True)

    entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
    if not entry:
        return await cq.answer("Dashboard expired — start a new /l, /ql, /qb or /y.", show_alert=True)

    if cq.data == "dash_close":
        entry["running"] = False
        GLOBAL_STATE["DASHBOARDS"].pop(chat_id, None)
        try:
            await cq.edit_message_text("📊 Dashboard closed.")
        except Exception:
            pass
        return await cq.answer("Closed.")

    if cq.data == "dash_ov":   # ☰ overview popup
        tasks = _dashboard_tasks(chat_id)
        c = lambda k: sum(1 for t in tasks if t["kind"] == k)
        snap = await asyncio.to_thread(_sys_snapshot)
        txt = (f"Tasks: {len(tasks)} (DL {c('⬇️ DL')} | UL {c('⬆️ UL')} | EX {c('🗜️ EX')})\n"
               f"CPU {snap['cpu']:.1f}% | RAM {snap['ram']:.1f}%\n"
               f"FREE {format_size(snap['free'])}\nUP {time_fmt(time.time() - BOT_START)}")
        return await cq.answer(txt, show_alert=True)

    if cq.data == "dash_prev":
        entry["page"] -= 1
    elif cq.data == "dash_next":
        entry["page"] += 1

    text, keyboard, page = await render_dashboard(app, chat_id, entry["page"])
    entry["page"] = page
    try:
        await cq.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        await cq.answer()
    except FloodWait as e:
        await cq.answer(f"Flood control: {e.value}s", show_alert=True)
    except Exception:
        await cq.answer()


# ================= STATS =================

async def stats_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    cpu, ram, free_disk, free_pct, uptime_str = await _get_system_stats(app)

    try:
        import py7zr
        sevenzip = f"py7zr {py7zr.version.__version__}"
    except Exception:
        sevenzip = "not installed"

    qb_line = "not running"
    if qb.QBIT_AVAILABLE and await qb.ping():
        qb_line = f"v{qb.version()}"

    try:
        du = await asyncio.to_thread(shutil.disk_usage, GLOBAL_STATE["DOWNLOAD_DIR"])
        disk = f"F: {du.free / 1024**3:.1f}GB | T: {du.total / 1024**3:.1f}GB [{du.used * 100 / du.total:.0f}% used]"
    except Exception:
        disk = "unavailable"

    text = (
        "🤖 <b>Bot Status Report</b>\n"
        "--- System ---\n"
        f"┠ CPU Load: <b>{cpu}%</b>\n"
        f"┠ RAM Usage: <b>{ram}%</b>\n"
        f"┖ Disk: <b>{disk}</b>\n\n"
        "--- Engines ---\n"
        f"┠ Aria2: <b>v{GLOBAL_STATE.get('ARIA_VER', '?')}</b>\n"
        f"┠ qBittorrent: <b>{qb_line}</b>\n"
        f"┠ yt-dlp: <b>{ytdl.version()}</b>\n"
        f"┠ Upload: <b>{_html.escape(str(GLOBAL_STATE.get('ENGINE_NAME', '?')))}</b>\n"
        f"┖ Extract: <b>{sevenzip}</b>\n\n"
        "--- Activity ---\n"
        f"┠ Active DLs: <b>{GLOBAL_STATE['DOWNLOAD_COUNT']}</b>\n"
        f"┠ Active ULs: <b>{GLOBAL_STATE['UPLOAD_COUNT']}</b>\n"
        f"┠ Total DL Time: <b>{time_fmt(GLOBAL_STATE['TOTAL_DOWNLOAD_TIME'])}</b>\n"
        f"┠ Total UL Time: <b>{time_fmt(GLOBAL_STATE['TOTAL_UPLOAD_TIME'])}</b>\n"
        f"┖ Uptime: <b>{uptime_str}</b>"
    )
    await reply_message_async(m, text, parse_mode=enums.ParseMode.HTML)




# ================= yt-dlp (/y) =================

def _cookie_hint(err):
    low = err.lower()
    if any(k in low for k in ("sign in", "cookies", "login required", "confirm you", "not a bot", "age")):
        return err + "\n\n🍪 This site wants a logged-in session: send a cookies.txt with /setcookies (see /settings)."
    return err


def _ytdl_tracker(url, outdir, choice, is_playlist, no_playlist, title, cookiefile=None):
    async def tracker(entry, gid):
        entry["name"] = title
        entry["name_locked"] = is_playlist          # keep the playlist title, not each video's
        fmt, codec, acq = choice
        try:
            await asyncio.to_thread(ytdl.download_blocking, url, outdir, fmt, codec, acq,
                                    is_playlist, entry, no_playlist, cookiefile)
        except Exception as e:
            await _rm_paths([outdir])
            if entry["cancel"]:
                return {"cancelled": True}
            return {"ok": False, "err": _cookie_hint(str(e))[:500], "name": title}
        if entry["cancel"]:
            await _rm_paths([outdir])
            return {"cancelled": True}
        files = await asyncio.to_thread(ytdl.collect_files, outdir)
        if not files:
            await _rm_paths([outdir])
            return {"ok": False, "err": "yt-dlp produced no files (private/removed/geo-blocked video?)", "name": title}
        return {"ok": True, "gid": gid, "name": title, "paths": files, "cleanup": [outdir]}
    return tracker


async def ytdl_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    if not ytdl.YTDL_AVAILABLE:
        return await reply_message_async(m, "❌ `yt-dlp` is not installed (`pip install yt-dlp[default]`).",
                                         parse_mode=enums.ParseMode.MARKDOWN)

    args = list(m.command[1:])
    no_playlist = "-np" in args
    args = [a for a in args if a != "-np"]
    quality = None
    if "-q" in args:
        i = args.index("-q")
        quality = args[i + 1] if i + 1 < len(args) else None
        args = args[:i] + args[i + 2:]
    args, zip_opt = _parse_zip(args)
    args, rename_to = _extract_rename_flag(args)
    url = args[0] if args else _link_from_reply(m)

    if not url or not url.startswith(("http://", "https://")):
        return await reply_message_async(
            m,
            "Usage (yt-dlp):\n"
            "`/y <url>`                — pick quality with buttons\n"
            "`/y <url> -q 720`         — skip the picker (`best` `audio` `mp3` `720` or a yt-dlp format)\n"
            "`/y <url> -n <name>`      — rename (single video)\n"
            "`/y <url> -np`            — single video only, ignore the playlist\n"
            "Or reply to a message containing a link with `/y`.",
            parse_mode=enums.ParseMode.MARKDOWN)

    first, uid = _who(m)
    cookiefile, cookie_src = await us.cookie_for(uid)
    status = await reply_message_async(
        m, "🔍 Fetching info…" + (f" 🍪 ({cookie_src} cookies)" if cookiefile else ""), parse_mode=None)
    try:
        info = await asyncio.to_thread(ytdl.extract_info, url, no_playlist, cookiefile)
    except Exception as e:
        err = _html.escape(_cookie_hint(str(e))[:700])
        if status:
            return await edit_message_async(status, f"❌ <b>yt-dlp failed</b>\n<code>{err}</code>",
                                            parse_mode=enums.ParseMode.HTML)
        return

    is_playlist = info.get("_type") == "playlist" or "entries" in info
    title = info.get("title") or info.get("id") or "yt-dlp"

    if quality is not None:
        choice = ytdl.parse_quality(quality)
    else:
        key = ytdl.new_pending(uid)
        rows = ytdl.picker_buttons(info, key)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])
        count = info.get("playlist_count") or len(info.get("entries") or [])
        head = (f"📋 <b>{_html.escape(title)}</b>\n<i>Playlist · {count} videos</i>" if is_playlist
                else f"🎬 <b>{_html.escape(title)}</b>")
        if status:
            await edit_message_async(status, f"{head}\n\nChoose quality (120s):",
                                     parse_mode=enums.ParseMode.HTML, reply_markup=kb)
        pend = ytdl.PENDING[key]
        try:
            await asyncio.wait_for(pend["event"].wait(), timeout=120)
        except asyncio.TimeoutError:
            ytdl.PENDING.pop(key, None)
            if status:
                await edit_message_async(status, "⌛ Timed out. Task cancelled.", parse_mode=None)
            return
        choice = pend["choice"]
        ytdl.PENDING.pop(key, None)
        if choice is None:
            if status:
                await edit_message_async(status, "🛑 Task cancelled.", parse_mode=None)
            return
    if status:
        try:
            await status.delete()
        except Exception:
            pass

    gid = f"yt{int(time.time() * 1000) % 10**9:x}"
    outdir = os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"ytdl_{gid}")
    os.makedirs(outdir, exist_ok=True)
    await _run_task(
        app, m, gid, _ytdl_tracker(url, outdir, choice, is_playlist, no_playlist, title, cookiefile),
        False, mode_tag="#Leech | #YtDlp", rename_to=rename_to, engine=f"yt-dlp v{ytdl.version()}",
        zip_opt=zip_opt
    )


async def ytq_callback_handler(app, cq: CallbackQuery):
    parts = (cq.data or "").split()
    if len(parts) < 3:
        return await cq.answer()
    pend = ytdl.PENDING.get(parts[1])
    if not pend:
        return await cq.answer("Expired.", show_alert=True)
    uid = cq.from_user.id if cq.from_user else 0
    if uid != pend["uid"] and not _is_owner(uid):
        return await cq.answer("Not your task.", show_alert=True)
    pend["choice"] = ytdl.decode_choice(parts[2])
    pend["event"].set()
    await cq.answer("Cancelled" if pend["choice"] is None else "Starting…")


# ================= FILENAME RULES (Leechv2 name swap / prefix / suffix) =================

_RULE_HELP = {
    "name_swap": (
        "Name Swap",
        "Regex swaps on the filename (extension untouched).\n"
        "Format: <code>pattern:replacement|pattern2:replacement2[:count]</code>\n"
        "Empty replacement removes the match.\n"
        "Example: <code>\\[.*?\\]::|www\\.\\S+::|Hindi:Tamil</code>\n"
        "(removes [brackets] and www urls, swaps Hindi → Tamil)",
    ),
    "prefix": ("Prefix", "Text put in front of the filename. <code>\\s</code> = space. Example: <code>@mychannel\\s-\\s</code>"),
    "caption": (
        "Caption",
        "Leech caption template (HTML ok, multi-line ok).\n"
        "Placeholders: <code>{filename} {size} {duration} {quality} {languages} {subtitles} {md5_hash}</code>\n"
        "Lines with video-only placeholders are dropped for non-video files.\n"
        "Replace text: <code>{filename|old:new}</code>",
    ),
    "suffix": ("Suffix", "Text put after the filename (before the extension). <code>\\s</code> = space. Example: <code>\\s[1080p]</code>"),
}


async def _rule_cmd(m, key):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    label, help_text = _RULE_HELP[key]
    parts = (m.text or "").split(None, 1)
    value = parts[1].strip() if len(parts) > 1 else ""
    cmd = {"name_swap": "setswap", "prefix": "setprefix", "suffix": "setsuffix", "caption": "setcaption"}[key]
    cur = (await us.get(uid)).get(key, "")
    if not value:
        return await reply_message_async(
            m, f"<b>{label}</b>\nCurrent: <code>{_html.escape(cur) if cur else 'none'}</code>\n\n{help_text}\n\n"
               f"Set: <code>/{cmd} &lt;value&gt;</code>   Clear: <code>/{cmd} clear</code>",
            parse_mode=enums.ParseMode.HTML)
    if value.lower() in ("clear", "none", "off", "del"):
        await us.set_rule(uid, key, "")
        return await reply_message_async(m, f"🗑 {label} cleared.", parse_mode=None)
    if key == "caption":
        err = validate_template(value)
        if err:
            return await reply_message_async(m, f"❌ {err}", parse_mode=None)
    if key == "name_swap":
        err = validate_swap(value)
        if err:
            return await reply_message_async(m, f"❌ {err}", parse_mode=None)
    await us.set_rule(uid, key, value)
    await reply_message_async(m, f"✅ {label} saved: <code>{_html.escape(value)}</code>", parse_mode=enums.ParseMode.HTML)


async def setswap_handler(_, m: Message):
    await _rule_cmd(m, "name_swap")


async def setprefix_handler(_, m: Message):
    await _rule_cmd(m, "prefix")


async def setsuffix_handler(_, m: Message):
    await _rule_cmd(m, "suffix")


async def setcaption_handler(_, m: Message):
    await _rule_cmd(m, "caption")



# ================= yt-dlp COOKIES (Leechv2 logic, stored in MongoDB) =================

async def setcookies_handler(app, m: Message):
    """Reply to a cookies.txt (or send it with the caption /setcookies). Owner: `/setcookies default`."""
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    want_default = len(m.command) > 1 and m.command[1].lower() == "default"
    if want_default and not _is_owner(uid):
        return await reply_message_async(m, "⛔ Only the owner can set the default cookies.", parse_mode=None)

    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if not doc:
        return await reply_message_async(
            m,
            "🍪 <b>yt-dlp cookies</b>\n"
            "Reply to a <code>cookies.txt</code> (Netscape format) with <code>/setcookies</code>, "
            "or send the file with that caption.\n"
            "Export it from a logged-in browser with a “Get cookies.txt” extension.\n"
            "Owner: <code>/setcookies default</code> sets the bot-wide file.\n"
            "Remove: <code>/delcookies</code>  (<code>/delcookies default</code>)\n\n"
            "<i>Do this in a private chat — cookies are your login.</i>",
            parse_mode=enums.ParseMode.HTML)
    try:
        buf = await app.download_media(doc, in_memory=True)
        data = bytes(buf.getbuffer())
    except Exception as e:
        return await reply_message_async(m, f"❌ Couldn't read the file: {e}", parse_mode=None)
    err = us.valid_cookies(data)
    if err:
        return await reply_message_async(m, f"❌ {err}", parse_mode=None)
    if want_default:
        await us.set_default_cookie(data)
        label = "Default cookies saved (everyone without personal cookies uses them)."
    else:
        await us.set_cookie(uid, data)
        label = "Your cookies saved. yt-dlp will use them for your downloads."
    # don't leave a login file lying in the chat
    for msg in (m, m.reply_to_message):
        if msg and msg.document:
            try:
                await msg.delete()
            except Exception:
                pass
    await app.send_message(m.chat.id, f"✅ {label}")


async def delcookies_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    if len(m.command) > 1 and m.command[1].lower() == "default":
        if not _is_owner(uid):
            return await reply_message_async(m, "⛔ Owner only.", parse_mode=None)
        await us.del_default_cookie()
        return await reply_message_async(m, "🗑 Default cookies removed.", parse_mode=None)
    await us.del_cookie(uid)
    await reply_message_async(m, "🗑 Your cookies removed.", parse_mode=None)


# ================= USER SETTINGS (upload type + thumbnail) =================

def _cookie_status(prefs):
    mine = bool(prefs.get("cookie"))
    dflt = us.default_cookie_exists()
    if mine and not prefs.get("use_default_cookie"):
        return "🍪 personal ✅ (in use)" + (" · default available" if dflt else "")
    if dflt:
        return "🍪 default ✅ (in use)" + (" · personal saved" if mine else "")
    return "❌ none"


def _settings_view(prefs):
    mode = prefs["mode"]
    text = (
        "⚙️ <b>Upload Settings</b>\n"
        f"┠ Upload as: <b>{'🎬 Video' if mode == 'video' else '📄 Document'}</b>\n"
        f"┠ Thumbnail: <b>{'✅ set' if prefs['thumb'] else '❌ none (auto from video)'}</b>\n"
        f"┠ Leech dump: <b>{('<code>%s</code>' % prefs['dump']) if prefs['dump'] else ('<code>%s</code> (global)' % config.LEECH_DUMP if config.LEECH_DUMP else '❌ none (same chat)')}</b>\n"
        f"┠ Prefix: <code>{_html.escape(prefs.get('prefix') or config.LEECH_PREFIX or 'none')}</code>\n"
        f"┠ Suffix: <code>{_html.escape(prefs.get('suffix') or config.LEECH_SUFFIX or 'none')}</code>\n"
        f"┠ Name Swap: <code>{_html.escape(prefs.get('name_swap') or config.LEECH_NAME_SWAP or 'none')}</code>\n"
        f"┠ Caption: <b>{'✅ custom' if prefs.get('caption') else 'default'}</b>\n"
        f"┠ yt-dlp cookies: <b>{_cookie_status(prefs)}</b>\n"
        f"┖ Saved in: <b>{us.backend()}</b>\n\n"
        "Thumb: reply to a photo with /setthumb\n"
        "Dump: /setdump &lt;chat_id&gt; (or /setdump here inside the group/channel)\n"
        "Names: /setswap  /setprefix  /setsuffix  /setcaption\n"
        "Cookies: reply to a cookies.txt with /setcookies"
    )
    rows = [[
        InlineKeyboardButton(("✅ " if mode == "doc" else "") + "📄 Document", callback_data="us_doc"),
        InlineKeyboardButton(("✅ " if mode == "video" else "") + "🎬 Video", callback_data="us_vid"),
    ]]
    if prefs["thumb"]:
        rows.append([InlineKeyboardButton("🖼 View thumb", callback_data="us_view"),
                     InlineKeyboardButton("🗑 Delete thumb", callback_data="us_del")])
    if prefs["dump"]:
        rows.append([InlineKeyboardButton("🗑 Clear dump", callback_data="us_deldump")])
    if prefs.get("cookie"):
        use = "Use default 🍪" if not prefs.get("use_default_cookie") and us.default_cookie_exists() else "Use mine 🍪"
        row = [InlineKeyboardButton("🗑 Delete my cookies", callback_data="us_ckdel")]
        if us.default_cookie_exists():
            row.insert(0, InlineKeyboardButton(use, callback_data="us_cktoggle"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="us_close")])
    return text, InlineKeyboardMarkup(rows)


async def settings_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    text, kb = _settings_view(await us.get(uid))
    await reply_message_async(m, text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)


async def setthumb_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    r = m.reply_to_message
    media = None
    if r:
        if r.photo:
            media = r
        elif r.document and (r.document.mime_type or "").startswith("image/"):
            media = r
    if not media:
        return await reply_message_async(m, "Reply to a **photo** with `/setthumb`.", parse_mode=enums.ParseMode.MARKDOWN)
    try:
        buf = await app.download_media(media, in_memory=True)
        await us.set_thumb(uid, bytes(buf.getbuffer()))
    except Exception as e:
        return await reply_message_async(m, f"❌ Thumbnail failed: {e}", parse_mode=None)
    await reply_message_async(m, "✅ Thumbnail saved. Applies to your next uploads.", parse_mode=None)


async def delthumb_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    await us.del_thumb(uid)
    await reply_message_async(m, "🗑 Thumbnail deleted.", parse_mode=None)


async def setdump_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.lower() in ("here", "this"):
        chat_id = m.chat.id
    else:
        try:
            chat_id = int(arg)
        except ValueError:
            return await reply_message_async(
                m, "Usage: `/setdump <chat_id>` (e.g. `-1001234567890`) or `/setdump here` inside the target group/channel.\n"
                   "Bot must be admin there. `/deldump` to clear.", parse_mode=enums.ParseMode.MARKDOWN)
    try:   # prove the bot can post there before saving
        probe = await app.send_message(chat_id, "✅ Leech dump set.")
        try:
            await probe.delete()
        except Exception:
            pass
    except Exception as e:
        return await reply_message_async(m, f"❌ Bot can't post in `{chat_id}`: {e}\nAdd the bot as admin first.",
                                         parse_mode=enums.ParseMode.MARKDOWN)
    await us.set_dump(uid, chat_id)
    await reply_message_async(m, f"✅ Leech dump set to `{chat_id}`. Your uploads go there.", parse_mode=enums.ParseMode.MARKDOWN)


async def deldump_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    _, uid = _who(m)
    await us.set_dump(uid, 0)
    await reply_message_async(m, "🗑 Leech dump cleared.", parse_mode=None)


async def settings_callback_handler(app, cq: CallbackQuery):
    uid = cq.from_user.id
    prefs = await us.get(uid)
    if cq.data == "us_close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        return await cq.answer()
    if cq.data == "us_view":
        if prefs["thumb"]:
            await app.send_photo(cq.message.chat.id, prefs["thumb"], caption="🖼 Your thumbnail")
        return await cq.answer()
    if cq.data == "us_doc":
        await us.set_mode(uid, "doc")
    elif cq.data == "us_vid":
        await us.set_mode(uid, "video")
    elif cq.data == "us_del":
        await us.del_thumb(uid)
    elif cq.data == "us_deldump":
        await us.set_dump(uid, 0)
    elif cq.data == "us_cktoggle":
        await us.set_use_default(uid, not prefs.get("use_default_cookie"))
    elif cq.data == "us_ckdel":
        await us.del_cookie(uid)
    text, kb = _settings_view(await us.get(uid))
    try:
        await cq.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass
    await cq.answer("Saved")



# ================= OWNER TOOLS: restart, log, authorize =================

RESTART_FILE = ".restartmsg"


async def restart_handler(_, m: Message):
    _, uid = _who(m)
    if not _is_owner(uid):
        return await reply_message_async(m, "⛔ Owner only.", parse_mode=None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes!", callback_data="botrestart confirm"),
                                InlineKeyboardButton("No!", callback_data="botrestart cancel")]])
    await reply_message_async(m, "<i>Are you really sure you want to restart the bot ?</i>",
                              parse_mode=enums.ParseMode.HTML, reply_markup=kb)


async def restart_callback_handler(app, cq: CallbackQuery):
    uid = cq.from_user.id if cq.from_user else 0
    if not _is_owner(uid):
        return await cq.answer("Owner only.", show_alert=True)
    if cq.data.endswith("cancel"):
        await cq.answer()
        try:
            return await cq.message.delete()
        except Exception:
            return
    await cq.answer("Restarting…")
    try:
        await cq.edit_message_text("<i>Restarting...</i>", parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass
    # drop every running transfer so nothing is orphaned in aria2 / qBittorrent
    try:
        for dl in await asyncio.to_thread(ARIA2_API.get_downloads):
            try:
                await asyncio.to_thread(ARIA2_API.remove, [dl], force=True, files=True)
            except Exception:
                pass
    except Exception:
        pass
    try:
        if qb.QBIT_AVAILABLE and await qb.ping():
            c = await qb.get_client()
            hashes = [t.hash for t in await c.torrents.info()]
            if hashes:
                await c.torrents.delete(hashes, True)
    except Exception:
        pass
    try:
        with open(RESTART_FILE, "w") as f:
            json.dump([cq.message.chat.id, cq.message.id], f)
    except Exception:
        pass
    await asyncio.sleep(1)
    # restart = pull the latest code first (update.py --run), then start the bot again
    if os.path.isfile("update.py"):
        os.execl(sys.executable, sys.executable, "update.py", "--run")
    os.execl(sys.executable, sys.executable, *sys.argv)


async def log_handler(app, m: Message):
    _, uid = _who(m)
    if not _is_owner(uid):
        return await reply_message_async(m, "⛔ Owner only.", parse_mode=None)
    if not os.path.exists("bot.log"):
        return await reply_message_async(m, "No bot.log yet.", parse_mode=None)
    tail = "bot.tail.log"
    def cut():
        with open("bot.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 1_500_000))
            data = f.read()
        with open(tail, "wb") as f:
            f.write(data)
    await asyncio.to_thread(cut)
    try:
        await app.send_document(m.chat.id, tail, caption="📑 Bot log (last 1.5MB)", reply_to_message_id=m.id)
    finally:
        await asyncio.to_thread(_rm_path, tail)


async def _auth_cmd(m: Message, grant: bool):
    _, uid = _who(m)
    if not _is_owner(uid):
        return await reply_message_async(m, "⛔ Owner only.", parse_mode=None)
    r = m.reply_to_message
    if r and r.from_user:
        target, label = r.from_user.id, f"user {r.from_user.id}"
        lst = config.AUTH_USERS
    else:
        target, label = m.chat.id, f"chat {m.chat.id}"
        lst = config.AUTH_CHATS
    if grant and target not in lst:
        lst.append(target)
    elif not grant and target in lst:
        lst.remove(target)
    await us.save_auth(config.AUTH_USERS, config.AUTH_CHATS)
    await reply_message_async(m, f"{'✅ Authorized' if grant else '🗑 Removed'} {label}.", parse_mode=None)


async def authorize_handler(_, m: Message):
    await _auth_cmd(m, True)


async def unauthorize_handler(_, m: Message):
    await _auth_cmd(m, False)


# Leechv2-style "/" menu. First name of each command is what Telegram lists.
BOT_COMMANDS = [
    ("leech", "[link/file] Leech files to Upload to Telegram"),
    ("unzipleech", "[link/file] Leech and extract archive"),
    ("qbleech", "[magnet/torrent] Leech files to Upload to Telegram using qbit"),
    ("qbunzipleech", "[magnet/torrent] QBit Leech and extract archive"),
    ("aria", "[magnet/torrent] Leech a magnet using Aria2"),
    ("ytdlleech", "[link] Leech YouTube, m3u8, Social Media and yt-dlp supported urls"),
    ("usetting", "User personal settings"),
    ("setthumb", "[reply to photo] Set your custom thumbnail"),
    ("setdump", "[chat_id|here] Set your leech dump chat"),
    ("setswap", "[pattern:replacement|...] Filename Name Swap (regex)"),
    ("setprefix", "[text] Filename prefix"),
    ("setsuffix", "[text] Filename suffix"),
    ("setcaption", "[template] Leech caption"),
    ("setcookies", "[reply to cookies.txt] Set yt-dlp cookies"),
    ("delcookies", "Remove your yt-dlp cookies"),
    ("ping", "Ping Bot to test Response Speed"),
    ("status", "[id/me] Tasks Status of Bot"),
    ("stats", "Bot, OS, Repo & System full Statistics"),
    ("cancelall", "Cancel all Tasks on the Bot"),
    ("help", "Detailed help usage of the bot"),
    ("authorize", "[OWNER] Authorize a chat or a replied user"),
    ("unauthorize", "[OWNER] Remove authorization"),
    ("log", "[OWNER] Get Bot Logs for Internal Working"),
    ("restart", "[OWNER] Reboot bot"),
]


async def on_startup(app):
    """Called once after the client connects: saved auth, command menu, restart notice."""
    users, chats = await us.load_auth()
    for u in users:
        if u not in config.AUTH_USERS:
            config.AUTH_USERS.append(u)
    for c in chats:
        if c not in config.AUTH_CHATS:
            config.AUTH_CHATS.append(c)

    if await us.load_default_cookie():
        print("✅ Default yt-dlp cookies restored from MongoDB")

    if config.SET_COMMANDS:
        try:
            await app.set_bot_commands([BotCommand(n, d) for n, d in BOT_COMMANDS])
            print(f"✅ Bot command menu set ({len(BOT_COMMANDS)} commands)")
        except Exception as e:
            print(f"⚠️ set_bot_commands failed: {e}")

    if os.path.exists(RESTART_FILE):
        try:
            chat_id, msg_id = json.load(open(RESTART_FILE))
            now = time.strftime("%d/%m/%y"), time.strftime("%I:%M:%S %p"), time.strftime("%Z") or "UTC"
            await app.edit_message_text(
                chat_id, msg_id,
                "<blockquote><b><i>Restarted Successfully!</i></b></blockquote>\n"
                f" • <b>Date:</b> {now[0]}\n • <b>Time:</b> {now[1]}\n • <b>TimeZone:</b> {now[2]}",
                parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            print(f"⚠️ restart notice failed: {e}")
        finally:
            _rm_path(RESTART_FILE)


# ================= REGISTRATION =================

def register_handlers(app, aria2_api, state_vars):
    global ARIA2_API, GLOBAL_STATE
    ARIA2_API = aria2_api
    GLOBAL_STATE = state_vars

    # command names follow Leechv2's BotCommands exactly
    cmd = filters.command
    app.on_message(cmd("start"))(start_handler)
    app.on_message(cmd(["help", "h"]))(help_handler)
    app.on_message(cmd("ping"))(ping_handler)
    app.on_message(cmd(["cancelall", "call"]))(cancelall_handler)
    app.on_message(filters.regex(r"^/(?:cancel|c)\d*_\S+"))(cancel_handler)
    app.on_message(cmd(["restart", "r", "restartall"]))(restart_handler)
    app.on_message(cmd("log"))(log_handler)
    app.on_message(cmd(["authorize", "a"]))(authorize_handler)
    app.on_message(cmd(["unauthorize", "ua"]))(unauthorize_handler)

    app.on_callback_query(filters.regex("^status_"))(status_callback_handler)
    app.on_callback_query(filters.regex("^dash_"))(dashboard_callback_handler)
    app.on_callback_query(filters.regex("^us_"))(settings_callback_handler)
    app.on_callback_query(filters.regex("^ytq "))(ytq_callback_handler)
    app.on_callback_query(filters.regex("^botrestart"))(restart_callback_handler)

    app.on_message(cmd(["settings", "usetting", "us"]))(settings_handler)
    app.on_message(cmd("setthumb"))(setthumb_handler)
    app.on_message(cmd("delthumb"))(delthumb_handler)
    app.on_message(cmd("setdump"))(setdump_handler)
    app.on_message(cmd("deldump"))(deldump_handler)
    app.on_message(cmd("setswap"))(setswap_handler)
    app.on_message(cmd("setprefix"))(setprefix_handler)
    app.on_message(cmd("setsuffix"))(setsuffix_handler)
    app.on_message(cmd("setcaption"))(setcaption_handler)
    app.on_message(cmd("setcookies"))(setcookies_handler)
    app.on_message(cmd("delcookies"))(delcookies_handler)

    @app.on_message(cmd(["leech", "l", "unzipleech", "uzl"]))
    async def leech_wrapper(client, message):
        await leech_handler(client, message)

    @app.on_message(cmd(["qbleech", "ql", "qb", "qbunzipleech", "quzl"]))
    async def qb_wrapper(client, message):
        await qb_handler(client, message)

    @app.on_message(cmd(["aria", "al"]))
    async def aria_wrapper(client, message):
        await ql_handler(client, message)

    @app.on_message(cmd(["ytdl", "y", "ytdlleech", "yl"]))
    async def ytdl_wrapper(client, message):
        await ytdl_handler(client, message)

    @app.on_message(cmd(["status", "s", "statusall", "sall"]))
    async def status_wrapper(client, message):
        await status_handler(client, message)

    @app.on_message(cmd(["stats", "st"]))
    async def stats_wrapper(client, message):
        await stats_handler(client, message)
