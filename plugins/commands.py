# plugins/commands.py

import os
import time
import asyncio
import subprocess
import html as _html

# NOTE: We need InlineKeyboardMarkup and InlineKeyboardButton for /status
from pyrogram import filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

import shutil
from pathlib import Path
import config
from plugins import qbit as qb
from plugins import usersettings as us

# ================= Global State and Helpers (Placeholders to be set by bot.py) =================
GLOBAL_STATE = {}
ARIA2_API = None

# Cap concurrent Telegram uploads (config.MAX_UPLOADS, default 4) — extra
# uploads queue instead of blocking, so a slow upload doesn't stall the rest.
UPLOAD_SEMAPHORE = asyncio.Semaphore(config.MAX_UPLOADS)

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

                def report_start(self, path, size):
                    pass  # size = compressed chunk size, not useful here

                def report_end(self, path, wrote_bytes):
                    done_bytes[0] += wrote_bytes
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


def make_caption(name, size):
    tpl = (config.LEECH_CAPTION or "<b>{filename}</b>").replace("\\n", "\n")
    try:
        return tpl.format(filename=_html.escape(name), size=format_size(size))
    except Exception:
        return f"<b>{_html.escape(name)}</b>"


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
    async with UPLOAD_SEMAPHORE:
        entry = GLOBAL_STATE["ACTIVE"].get(gid)
        parts = [file_path]
        ok, err = False, ""
        GLOBAL_STATE["UPLOAD_COUNT"] += 1
        try:
            if entry is None or entry.get("cancel"):
                raise Exception(CANCEL_MSG)

            limit = config.SPLIT_SIZE
            if limit and file_size > limit:
                entry["status"] = "Splitting"
                parts = await asyncio.to_thread(split_file, file_path, limit)
                await asyncio.to_thread(_rm_path, file_path)   # free disk, parts hold the data
                entry["status"] = "Upload"

            total = sum(os.path.getsize(p) for p in parts)
            offset = 0
            prefs = await us.get(entry.get("user_id", 0))
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
                    await _send_with_retry(app, target_chat, part, gid, offset, total,
                                           make_caption(pname, psize), kind, thumb, extra)
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
    PARSE_MODE = enums.ParseMode.MARKDOWN
    user_name = m.from_user.first_name if m.from_user else "there"
    start_message = (
        f"👋 Hello, **{user_name}**!\n"
        "I am a fast leech bot — **Aria2** + **qBittorrent** download, **Telegram** upload.\n\n"
        "**📚 How to Use Me:**\n"
        "┠ Direct link: `/l <URL>`\n"
        "┠ Leech & extract: `/l <URL> -e`\n"
        "┠ Rename: `/l <URL> -n <new name>`\n"
        "┠ Magnet / .torrent via **qBittorrent**: `/qb <magnet|.torrent URL>` or reply to a `.torrent` with `/qb`\n"
        "┠ Magnet / .torrent via **Aria2**: `/ql <magnet>` or reply to a `.torrent` with `/ql`\n"
        "┠ Reply to a message containing a link with `/l` or `/qb`\n"
        "┠ Active transfers: `/status`\n"
        "┠ Upload type (Document / Video) + thumbnail: `/settings` (`/st`), `/setthumb`, `/delthumb`\n┠ Leech dump chat: `/setdump <chat_id|here>`, `/deldump`\n"
        "┠ System stats: `/stats`  •  Latency: `/ping`\n"
        "┠ Cancel one task: `/cancel_<gid>`\n"
        "┖ Cancel everything (owner): `/cancelall`\n\n"
        f"**⚡ Uploads:** up to {config.MAX_UPLOADS} in parallel, auto-retry on network errors, "
        "files over the Telegram limit are split into `.001 .002 …` parts.\n\n"
        "**📦 Extract (`-e`):** `.zip` `.7z` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` (no `.rar`)\n\n"
        "🚀 Happy Leeching!"
    )
    await reply_message_async(m, start_message, parse_mode=PARSE_MODE)


async def ping_handler(_, m):
    t0 = time.time()
    msg = await reply_message_async(m, "🏓 Pong!", parse_mode=None)
    if msg:
        await edit_message_async(msg, f"🏓 Pong! `{(time.time() - t0) * 1000:.0f} ms`",
                                 parse_mode=enums.ParseMode.MARKDOWN)


# ----- Aria2: /l (direct link) -----

async def leech_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)

    args = list(m.command[1:])
    extract_mode = "-e" in args
    args = [a for a in args if a != "-e"]
    args, rename_to = _extract_rename_flag(args)
    url = args[0] if args else _link_from_reply(m)

    if not url:
        return await reply_message_async(
            m,
            "Usage:\n"
            "`/l <direct_url>`             — Leech and upload\n"
            "`/l <direct_url> -e`          — Leech, extract, then upload all files\n"
            "`/l <direct_url> -n <name>`   — Leech and rename before upload\n"
            "Or reply to a message containing a link with `/l`.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

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
        rename_to=rename_to, engine=f"Aria2 v{GLOBAL_STATE.get('ARIA_VER', '?')}"
    )


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
            "`/ql <magnet_link>`            — Leech a magnet link\n"
            "`/ql <magnet_link> -e`         — Leech magnet, extract, then upload\n"
            "`/ql <magnet_link> -n <name>`  — Leech magnet and rename before upload\n"
            "Reply to a `.torrent` file with `/ql` to leech it\n"
            "Want qBittorrent instead? Use `/qb`.",
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
        rename_to=rename_to, engine=f"Aria2 v{GLOBAL_STATE.get('ARIA_VER', '?')}"
    )


# ----- qBittorrent: /qb -----

async def qb_handler(app, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    if not qb.QBIT_AVAILABLE:
        return await reply_message_async(m, "❌ `aioqbt` is not installed (`pip install aioqbt`).",
                                         parse_mode=enums.ParseMode.MARKDOWN)

    args = list(m.command[1:])
    extract_mode = "-e" in args
    args = [a for a in args if a != "-e"]
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
        rename_to=rename_to, engine=f"qBittorrent v{qb.version()}", extra_state={"engine_id": "qbit", "hash": h}
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
                entry["extra"] = f"Seeders: {dl.num_seeders} | Conn: {dl.connections}"
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
            entry["extra"] = f"Seeders: {t.num_seeds} | Peers: {t.num_leechs}"

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


async def _run_task(app, m, gid, tracker, extract_mode, mode_tag, rename_to, engine, extra_state=None):
    GLOBAL_STATE["DOWNLOAD_COUNTER"] += 1
    start_time = time.time()
    first, uid = _who(m)

    await _ensure_dashboard(app, m.chat.id, force_new=True)

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
                              mode_tag, rename_to, res["name"], start_time)
    except Exception as e:
        print(f"❌ post-download crashed: {e!r}")
        await _rm_paths(res.get("cleanup", []) + res.get("paths", []))
        await reply_message_async(m, f"❌ Post-processing failed: {e}", parse_mode=None)


async def _after_download(app, m, gid, paths, cleanup, extract_mode, mode_tag, rename_to, dl_name, start_time):
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

    # ── 2. rename: -n wins for a single file, else strip site-tag junk ──────
    renamed = []
    for p in files:
        original = os.path.basename(p)
        if rename_to and len(files) == 1:
            _, orig_ext = os.path.splitext(original)
            _, new_ext = os.path.splitext(rename_to)
            new_name = rename_to if new_ext else f"{rename_to}{orig_ext}"
        else:
            new_name = clean_filename(original)
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
    jobs = []   # (path, name)
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
    else:
        jobs = files

    if not jobs:
        await _rm_paths(cleanup)
        return await reply_message_async(m, "⚠️ Nothing to upload (extraction produced no files).", parse_mode=None)

    # ── 4. queue uploads; semaphore caps real concurrency ──────────────────
    await _ensure_dashboard(app, m.chat.id)
    upload_jobs = []
    for idx, p in enumerate(jobs, 1):
        original = os.path.basename(p)
        name = clean_filename(original) if extract_mode else original
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
            "progress": {"done": 0, "total": size, "speed": 0},
            "base_gid": gid, "cleanup": cleanup,
            "chat_id": m.chat.id, "start_time": time.time(),
            "user_first": first, "user_id": uid, "mode_tag": mode_tag,
            "status": "Upload", "engine": GLOBAL_STATE.get("ENGINE_NAME", "Pyrogram"),
        }
        upload_jobs.append((p, name, size, sub))

    results = await asyncio.gather(*[
        upload_file(app, target_chat, p, name, size, sub) for p, name, size, sub in upload_jobs
    ])

    # ── 5. cleanup + completion report ─────────────────────────────────────
    await _rm_paths(cleanup)

    ok_count = sum(1 for ok, _ in results if ok)
    cancelled = any(err == "cancelled" for ok, err in results if not ok)
    if cancelled:
        return   # cancel handler already told the user

    total_size = sum(j[2] for j in upload_jobs)
    elapsed = time_fmt(time.time() - start_time)
    lines = [
        "✅ <b>Leech Completed</b>" if ok_count == len(results) else "⚠️ <b>Leech Finished With Errors</b>",
        f"<b>{_html.escape(dl_name)}</b>",
        f"┌ <b>Task Size:</b> {format_size(total_size)}",
        f"├ <b>Time Taken:</b> {elapsed}",
        f"├ <b>Mode:</b> {mode_tag}",
        f"├ <b>Total Files:</b> {ok_count}/{len(results)} uploaded",
        f"└ <b>Task By:</b> {_html.escape(first)}",
    ]
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

    # ── download: the engine tracker sees the flag and cleans up itself ──
    return f"🛑 Cancelling download <b>{_html.escape(str(info.get('name', task_id)))}</b>…"


async def cancel_handler(_, m: Message):
    if not is_authorized(m):
        return await _deny(m)
    token = ((m.text or "").split() or [""])[0].split("@")[0]       # drop args and @botname
    raw = token.replace("/cancel", "", 1)
    task_id = raw.split("_", 1)[1] if "_" in raw else raw
    if not task_id or task_id == "all":
        return
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

DASH_PAGE_SIZE        = 3
DASH_REFRESH_INTERVAL = 5   # seconds
DASH_IDLE_STOP        = 3   # empty refreshes in a row before the loop parks itself


def _dashboard_tasks(chat_id):
    """Snapshot of all active DL / EX / UL tasks for this chat (insertion order)."""
    tasks = []
    for gid, info in list(GLOBAL_STATE["ACTIVE"].items()):
        if info.get("chat_id") != chat_id or "kind" not in info:
            continue
        prog = info.get("progress") or {}
        kind = info["kind"]
        default_status = {"⬇️ DL": "Download", "⬆️ UL": "Upload"}.get(kind, "Extract")
        tasks.append({
            "gid": gid, "kind": kind, "name": info.get("name", "?"),
            "done": prog.get("done", 0), "total": prog.get("total", 0), "speed": prog.get("speed", 0),
            "status": info.get("status", default_status),
            "engine": info.get("engine", "?"),
            "mode_tag": info.get("mode_tag", ""),
            "extra": info.get("extra", ""),
            "start_time": info.get("start_time", time.time()),
            "user_first": info.get("user_first", "?"),
            "user_id": info.get("user_id", "?"),
        })
    return tasks


async def _get_system_stats(app):
    """Cheap live system snapshot for the Bot Stats block."""
    cpu, ram, free_disk, free_pct = "N/A", "N/A", "N/A", "N/A"
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["sh", "-c", "top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}'"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            cpu = f"{float(r.stdout.strip()):.1f}"
    except Exception:
        pass
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["sh", "-c", "free -m | awk 'NR==2{printf \"%.1f\", $3*100/$2}'"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            ram = r.stdout.strip()
    except Exception:
        pass
    try:
        du = await asyncio.to_thread(shutil.disk_usage, GLOBAL_STATE["DOWNLOAD_DIR"])
        free_disk = f"{du.free / 1024**3:.1f}"
        free_pct = f"{du.free * 100 / du.total:.0f}"
    except Exception:
        pass
    uptime_str = time_fmt(time.time() - getattr(app, "start_time", time.time()))
    return cpu, ram, free_disk, free_pct, uptime_str


async def render_dashboard(app, chat_id, page):
    tasks = _dashboard_tasks(chat_id)
    total_pages = max(1, (len(tasks) + DASH_PAGE_SIZE - 1) // DASH_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * DASH_PAGE_SIZE: page * DASH_PAGE_SIZE + DASH_PAGE_SIZE]

    dl_speed_total = sum(t["speed"] for t in tasks if t["kind"] == "⬇️ DL")
    ul_speed_total = sum(t["speed"] for t in tasks if t["kind"] == "⬆️ UL")

    lines = []
    if not page_tasks:
        lines.append("✅ No active tasks.")
    else:
        for t in page_tasks:
            name = _html.escape(t["name"][:70])
            elapsed = time_fmt(int(time.time() - t["start_time"]))
            eta_sec = (t["total"] - t["done"]) / t["speed"] if t["speed"] > 0 else 0
            lines.append(f"<b>{name}</b>")
            lines.append(f"   {progress_bar(t['done'], t['total'])}")
            lines.append(f"├ <b>Processed:</b> {format_size(t['done'])} of {format_size(t['total'])}")
            lines.append(f"├ <b>Status:</b> {t['status']} | <b>ETA:</b> {time_fmt(eta_sec)}")
            lines.append(f"├ <b>Speed:</b> {format_speed(t['speed'])} | <b>Elapsed:</b> {elapsed}")
            if t["extra"]:
                lines.append(f"├ {_html.escape(t['extra'])}")
            lines.append(f"├ <b>Engine:</b> {_html.escape(str(t['engine']))}")
            if t["mode_tag"]:
                lines.append(f"├ <b>Mode:</b> {_html.escape(t['mode_tag'])}")
            lines.append(f"├ <b>User:</b> {_html.escape(str(t['user_first']))} | <b>ID:</b> {t['user_id']}")
            lines.append(f"└ /cancel_{t['gid']}")
            lines.append("")

    cpu, ram, free_disk, free_pct, uptime_str = await _get_system_stats(app)
    lines.append("⬡ <b>Bot Stats</b>")
    lines.append(f"├ <b>CPU:</b> {cpu}% | <b>F:</b> {free_disk}GB [{free_pct}%]")
    lines.append(f"├ <b>RAM:</b> {ram}% | <b>UPTIME:</b> {uptime_str}")
    lines.append(f"└ <b>DL:</b> {format_speed(dl_speed_total)} | <b>UL:</b> {format_speed(ul_speed_total)}")
    lines.append("")
    lines.append(f"<b>PAGES</b> {page + 1}/{total_pages}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Prev", callback_data="dash_prev"),
            InlineKeyboardButton("🔄 Refresh", callback_data="dash_refresh"),
            InlineKeyboardButton("Next ➡️", callback_data="dash_next"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="dash_close")]
    ])
    return "\n".join(lines), keyboard, page


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

    text, keyboard, page = await render_dashboard(app, chat_id, 0)
    msg = await app.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
    gen = time.time()
    GLOBAL_STATE["DASHBOARDS"][chat_id] = {"msg": msg, "page": 0, "running": True, "gen": gen}
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
        return await cq.answer("Dashboard expired — start a new /l, /ql or /qb.", show_alert=True)

    if cq.data == "dash_close":
        entry["running"] = False
        GLOBAL_STATE["DASHBOARDS"].pop(chat_id, None)
        try:
            await cq.edit_message_text("📊 Dashboard closed.")
        except Exception:
            pass
        return await cq.answer("Closed.")

    if cq.data == "dash_prev":
        entry["page"] = max(0, entry["page"] - 1)
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



# ================= USER SETTINGS (upload type + thumbnail) =================

def _settings_view(prefs):
    mode = prefs["mode"]
    text = (
        "⚙️ <b>Upload Settings</b>\n"
        f"┠ Upload as: <b>{'🎬 Video' if mode == 'video' else '📄 Document'}</b>\n"
        f"┠ Thumbnail: <b>{'✅ set' if prefs['thumb'] else '❌ none (auto from video)'}</b>\n"
        f"┠ Leech dump: <b>{('<code>%s</code>' % prefs['dump']) if prefs['dump'] else ('<code>%s</code> (global)' % config.LEECH_DUMP if config.LEECH_DUMP else '❌ none (same chat)')}</b>\n"
        f"┖ Saved in: <b>{us.backend()}</b>\n\n"
        "Thumb: reply to a photo with /setthumb\n"
        "Dump: /setdump &lt;chat_id&gt; (or /setdump here inside the group/channel)"
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
    text, kb = _settings_view(await us.get(uid))
    try:
        await cq.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass
    await cq.answer("Saved")


# ================= REGISTRATION =================

def register_handlers(app, aria2_api, state_vars):
    global ARIA2_API, GLOBAL_STATE
    ARIA2_API = aria2_api
    GLOBAL_STATE = state_vars

    app.on_message(filters.command(["start", "help"]))(start_handler)
    app.on_message(filters.command("ping"))(ping_handler)
    app.on_message(filters.command("cancelall"))(cancelall_handler)
    app.on_message(filters.regex(r"^/cancel(?!all)"))(cancel_handler)
    app.on_callback_query(filters.regex("^status_"))(status_callback_handler)
    app.on_callback_query(filters.regex("^dash_"))(dashboard_callback_handler)
    app.on_callback_query(filters.regex("^us_"))(settings_callback_handler)
    app.on_message(filters.command(["settings", "usettings", "st"]))(settings_handler)
    app.on_message(filters.command("setthumb"))(setthumb_handler)
    app.on_message(filters.command("delthumb"))(delthumb_handler)
    app.on_message(filters.command("setdump"))(setdump_handler)
    app.on_message(filters.command("deldump"))(deldump_handler)

    @app.on_message(filters.command(["l", "leech"]))
    async def leech_wrapper(client, message):
        await leech_handler(client, message)

    @app.on_message(filters.command("ql"))
    async def ql_wrapper(client, message):
        await ql_handler(client, message)

    @app.on_message(filters.command(["qb", "qbleech"]))
    async def qb_wrapper(client, message):
        await qb_handler(client, message)

    @app.on_message(filters.command("status"))
    async def status_wrapper(client, message):
        await status_handler(client, message)

    @app.on_message(filters.command("stats"))
    async def stats_wrapper(client, message):
        await stats_handler(client, message)
