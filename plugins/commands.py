# plugins/commands.py

import os
import time
import asyncio
import subprocess
import html as _html
from datetime import timedelta

# NOTE: We need InlineKeyboardMarkup and InlineKeyboardButton for /status
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

# ================= Global State and Helpers (Placeholders to be set by bot.py) =================
GLOBAL_STATE = {}
ARIA2_API = None

# Cap concurrent Telegram uploads at 4 — extra uploads queue instead of
# blocking sequentially, so a slow 3-4min upload doesn't stall the rest.
UPLOAD_SEMAPHORE = asyncio.Semaphore(4)

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
        if progress_gid and progress_gid in GLOBAL_STATE["ACTIVE"]:
            GLOBAL_STATE["ACTIVE"][progress_gid]["progress"] = {"done": done, "total": total, "speed": speed}

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

                for m in members:
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

# --- Upload Progress Function (Runs in a separate thread) ---

def upload_progress(current, total, gid, start_time, name, parse_mode, loop, download_index, user_first, user_id):
    if total == 0: return

    if GLOBAL_STATE["ACTIVE"].get(gid, {}).get("cancel", False):
        raise Exception("Upload manually cancelled by user.")

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0

    # Store live stats for the dashboard even when the per-task message
    # edit below is throttled — dashboard reads this directly.
    if gid in GLOBAL_STATE["ACTIVE"]:
        GLOBAL_STATE["ACTIVE"][gid]["progress"] = {"done": current, "total": total, "speed": speed}
    # Progress is now shown live in the single dashboard message — no more
    # per-task edit spam here. Only "queued/complete/failed" get an edit.

# --- Upload Handler (Runs in the main event loop) ---

async def upload_file(app, msg, file_path, name, file_size, loop, gid, download_index, user_first, user_id):
    # Queues past 4 concurrent uploads instead of blocking the caller —
    # slow uploads (3-4min) no longer stall other tasks waiting to start.
    async with UPLOAD_SEMAPHORE:
        GLOBAL_STATE["UPLOAD_COUNT"] += 1
        try:
            await app.send_document(
                msg.chat.id, 
                file_path,
                caption=f"✅ **{name}**\nSize: {format_size(file_size)}",
                progress=upload_progress, 
                progress_args=(gid, time.time(), name, enums.ParseMode.MARKDOWN, loop, download_index, user_first, user_id)
            )
            await edit_message_async(msg, f"✅ Upload complete for **{name}**!", parse_mode=enums.ParseMode.MARKDOWN)
        
        except Exception as e:
            cancellation_message = "Upload manually cancelled by user."
            
            if cancellation_message in str(e):
                print(f"Upload GID {gid} was gracefully cancelled.")
            
            elif not GLOBAL_STATE["ACTIVE"].get(gid, {}).get("cancel", False):
                print(f"❌ Upload GID {gid} failed unexpectedly: {e}")
                await edit_message_async(msg, f"❌ Upload failed: {str(e)}", parse_mode=None)
        
        finally:
            GLOBAL_STATE["UPLOAD_COUNT"] -= 1
            GLOBAL_STATE["ACTIVE"].pop(gid, None) 
            if os.path.exists(file_path):
                await asyncio.to_thread(os.remove, file_path)

# ================= STATUS FUNCTIONS =================

def get_status_keyboard():
    """Returns an inline keyboard with a Refresh button."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh Status", callback_data="status_refresh")
        ]
    ])

def get_all_active_status(app):
    status_lines = []
    
    # Calculate Total Uptime
    uptime_sec = time.time() - app.start_time
    uptime_str = time_fmt(uptime_sec)
    
    # Summary Header
    status_lines.append("🚀 **Active Transfer Status**")
    status_lines.append("----------------------------")
    
    # System & Bot Stats
    active_download = GLOBAL_STATE["DOWNLOAD_COUNT"]
    active_upload = GLOBAL_STATE["UPLOAD_COUNT"]
    
    status_lines.append(f"🟢 **Bot Uptime**: {uptime_str}")
    status_lines.append(f"⬇️ **Active DLs**: {active_download} | ⬆️ **Active ULs**: {active_upload}")
    status_lines.append("----------------------------")

    if not GLOBAL_STATE["ACTIVE"]:
        status_lines.append("✅ No active downloads or uploads at this time.")
        return "\n".join(status_lines)

    # Detailed Task List
    for gid, task_info in GLOBAL_STATE["ACTIVE"].items():
        is_upload = "file_path" in task_info
        
        if is_upload:
            name = task_info.get("name", "Unknown Upload")
            status_lines.append(f"⬆️ **UL** - **{name}**")
            status_lines.append(f"  Status: Uploading | /cancel_{gid}")
            
        else:
            try:
                dl = ARIA2_API.get_download(gid)
                
                if dl.is_complete or dl.is_removed or dl.has_failed:
                    continue 

                done = dl.completed_length
                total = dl.total_length
                speed = dl.download_speed
                
                progress_output = progress_bar(done, total, size=15)
                
                status_lines.append(f"⬇️ **DL** - **{dl.name}**")
                status_lines.append(f"  {progress_output} {format_size(done)}/{format_size(total)}")
                status_lines.append(f"  Speed: {format_speed(speed)} | /cancel_{gid}")
                
            except Exception:
                status_lines.append(f"⚠️ **DL** - GID `{gid}` (Status Update Failed)")
    
    return "\n".join(status_lines)


# ================= COMMAND HANDLERS =================

async def start_handler(_, m):
    PARSE_MODE = enums.ParseMode.MARKDOWN
    user_name = m.from_user.first_name
    start_message = (
        f"👋 Hello, **{user_name}**!\n"
        "I am a fast Aria2 Leech Bot designed to download files directly from URLs and upload them to Telegram.\n\n"
        "**📚 How to Use Me:**\n"
        "┠ To start a download: `/l <Direct_URL>`\n"
        "┠ To leech & extract: `/l <Direct_URL> -e`\n"
        "┠ To rename while leeching: `/l <Direct_URL> -n <new name>`\n"
        "┠ To leech a magnet: `/ql <magnet_link>`\n"
        "┠ To leech a .torrent: reply to the file with `/ql`\n"
        "┠ To check active transfers: `/status`\n"
        "┠ To view system stats: `/stats`\n"
        "┖ To cancel an active task: `/cancel<index>_<gid>`\n\n"
        "**⚡ Uploads:** up to 4 run in parallel — no waiting on slow files.\n\n"
        "**📊 Dashboard:** opens automatically on every `/l` or `/ql` — one live message tracks all downloads, uploads, and extractions at once.\n\n"
        "**📦 Extract Mode (`-e` flag):**\n"
        "┠ Supports: `.zip` `.7z` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz`\n"
        "┖ Powered by **py7zr** / **tarfile** (no `.rar`, needs `unrar` binary)\n\n"
        "**ℹ️ Supported URLs:**\n"
        "┖ Direct file links (HTTP/HTTPS) and Torrent files.\n\n"
        "🚀 Happy Leeching!"
    )
    await reply_message_async(m, start_message, parse_mode=PARSE_MODE)


async def leech_handler(app, m: Message):
    # ── Parse args: /l <url> [-e] ──────────────────────────────────────────
    if len(m.command) < 2:
        return await reply_message_async(
            m,
            "Usage:\n"
            "`/l <direct_url>`             — Leech and upload\n"
            "`/l <direct_url> -e`          — Leech, extract (7zip), then upload all files\n"
            "`/l <direct_url> -n <name>`   — Leech and rename before upload",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    args = m.command[1:]          # Everything after /l
    extract_mode = "-e" in args
    args = [a for a in args if a != "-e"]
    args, rename_to = _extract_rename_flag(args)
    url_parts = args

    if not url_parts:
        return await reply_message_async(m, "❌ No URL provided.", parse_mode=None)

    url = url_parts[0]

    # ── Start Aria2 download ───────────────────────────────────────────────
    try:
        options = {
            "dir": GLOBAL_STATE["DOWNLOAD_DIR"],
            "max-connection-per-server": "4",
            "min-split-size": "1M",
            "split": "4",
            "max-concurrent-downloads": "10"
        }
        dl = await asyncio.to_thread(ARIA2_API.add_uris, [url], options)
        gid = dl.gid
    except Exception as e:
        return await reply_message_async(m, f"Failed to start download: {e}", parse_mode=None)

    await _process_task(
        app, m, dl, gid, extract_mode,
        mode_tag="#Extract | #7zip" if extract_mode else "#Leech | #Aria2",
        rename_to=rename_to
    )


async def ql_handler(app, m: Message):
    """
    /ql <magnet_link> [-e]        — leech a magnet
    Reply to a .torrent file with /ql [-e]  — leech that torrent
    """
    args = m.command[1:] if len(m.command) > 1 else []
    extract_mode = "-e" in args
    args = [a for a in args if a != "-e"]
    args, rename_to = _extract_rename_flag(args)
    text_parts = args

    torrent_path   = None
    magnet_uri     = text_parts[0] if text_parts else None
    replied        = m.reply_to_message

    # ── Case 1: replying to a .torrent file ─────────────────────────────────
    if replied and replied.document:
        fname = (replied.document.file_name or "").lower()
        is_torrent_doc = fname.endswith(".torrent") or replied.document.mime_type == "application/x-bittorrent"
        if is_torrent_doc:
            try:
                torrent_path = await app.download_media(
                    replied,
                    file_name=os.path.join(GLOBAL_STATE["DOWNLOAD_DIR"], f"ql_{replied.document.file_unique_id}.torrent")
                )
            except Exception as e:
                return await reply_message_async(m, f"❌ Failed to fetch .torrent file: {e}", parse_mode=None)

    if not torrent_path and not magnet_uri:
        return await reply_message_async(
            m,
            "Usage:\n"
            "`/ql <magnet_link>`            — Leech a magnet link\n"
            "`/ql <magnet_link> -e`         — Leech magnet, extract, then upload\n"
            "`/ql <magnet_link> -n <name>`  — Leech magnet and rename before upload\n"
            "Reply to a `.torrent` file with `/ql` (or `/ql -e` / `/ql -n <name>`) to leech it",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    if magnet_uri and not magnet_uri.startswith("magnet:"):
        return await reply_message_async(m, "❌ Not a valid magnet link (must start with `magnet:`).", parse_mode=enums.ParseMode.MARKDOWN)

    # Extra public trackers = more peers found faster, especially for
    # magnets with few/no trackers of their own (udp + http + wss mix).
    EXTRA_TRACKERS = ",".join([
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.tracker.cl:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://exodus.desync.com:6969/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://explodie.org:6969/announce",
        "udp://tracker.dler.org:6969/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker-udp.gbitt.info:80/announce",
        "udp://tracker.theoks.net:6969/announce",
        "udp://tracker.bittor.pw:1337/announce",
        "udp://ipv4.tracker.harry.lu:80/announce",
        "udp://opentracker.i2p.rocks:6969/announce",
        "udp://tracker.moeking.me:6969/announce",
        "udp://tracker.zerobytes.xyz:1337/announce",
        "udp://retracker01-msk-virt.corbina.net:80/announce",
        "http://tracker.openbittorrent.com:80/announce",
        "http://tracker.opentrackr.org:1337/announce",
        "https://tracker.gbitt.info:443/announce",
        "https://tracker.tamersunion.org:443/announce",
        "wss://tracker.openwebtorrent.com",
        "wss://tracker.btorrent.xyz",
    ])

    options = {
        "dir": GLOBAL_STATE["DOWNLOAD_DIR"],
        "max-connection-per-server": "4",
        "seed-time": "0",
        "bt-stop-timeout": "600",
        "bt-tracker": EXTRA_TRACKERS,
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
        gid = dl.gid
    except Exception as e:
        return await reply_message_async(m, f"Failed to start torrent/magnet download: {e}", parse_mode=None)

    await _process_task(
        app, m, dl, gid, extract_mode,
        mode_tag="#Extract | #Torrent" if extract_mode else "#Leech | #Magnet",
        rename_to=rename_to
    )


async def _process_task(app, m: Message, dl, gid, extract_mode, mode_tag, rename_to=None):
    """
    Shared tail: progress-tracks an already-added aria2 download (URL, magnet,
    or .torrent) through completion, then extracts (optional) and uploads.
    Handles magnet metadata -> real-download GID handoff automatically.
    """
    download_index = GLOBAL_STATE["DOWNLOAD_COUNTER"]
    GLOBAL_STATE["DOWNLOAD_COUNTER"] += 1

    start_time = time.time()

    await _ensure_dashboard(app, m.chat.id, force_new=True)
    msg = GLOBAL_STATE["DASHBOARDS"][m.chat.id]["msg"]   # reuse dashboard msg for rare one-off edits

    GLOBAL_STATE["ACTIVE"][gid] = {
        "cancel": False, "start_time": start_time, "chat_id": m.chat.id,
        "user_first": m.from_user.first_name, "user_id": m.from_user.id, "mode_tag": mode_tag
    }
    GLOBAL_STATE["DOWNLOAD_COUNT"] += 1

    # ── Download progress loop — dashboard shows live progress, so this
    # loop just tracks state silently instead of spamming a separate edit. ──
    while not dl.is_complete:
        if GLOBAL_STATE["ACTIVE"].get(gid, {}).get("cancel") or dl.is_removed or dl.has_failed:
            await edit_message_async(msg, f"Download {gid} finished, removed, or failed.", parse_mode=None)
            break

        await asyncio.to_thread(dl.update)

        # ── Magnet metadata handoff: once metadata resolves, aria2 spawns a
        # new GID for the real torrent download. Follow it transparently.
        followed = getattr(dl, "followed_by_ids", None)
        if dl.is_complete and followed:
            new_gid = followed[0]
            GLOBAL_STATE["ACTIVE"].pop(gid, None)
            gid = new_gid
            dl = await asyncio.to_thread(ARIA2_API.get_download, gid)
            GLOBAL_STATE["ACTIVE"][gid] = {
                "cancel": False, "start_time": start_time, "chat_id": m.chat.id,
                "user_first": m.from_user.first_name, "user_id": m.from_user.id, "mode_tag": mode_tag
            }
            continue

        await asyncio.sleep(3)

    GLOBAL_STATE["DOWNLOAD_COUNT"] -= 1
    GLOBAL_STATE["ACTIVE"].pop(gid, None)

    # ── Post-download checks ───────────────────────────────────────────────
    if not (dl.is_complete and dl.files and dl.files[0]):
        if dl.has_failed:
            return await edit_message_async(
                msg,
                f"❌ Download **{dl.name}** failed.\nReason: {dl.error_message}",
                parse_mode=enums.ParseMode.MARKDOWN
            )
        return

    file_path = dl.files[0].path

    if not file_path or not os.path.exists(file_path):
        return await edit_message_async(msg, "❌ Download complete but file missing.", parse_mode=None)

    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise ValueError("File empty")
    except Exception:
        return await edit_message_async(msg, "❌ File corrupted or empty.", parse_mode=None)

    # ── Rename: explicit -n name wins, else auto-clean site-tag junk ────────
    original_name = os.path.basename(file_path)
    if rename_to:
        _, orig_ext = os.path.splitext(original_name)
        new_base, new_ext = os.path.splitext(rename_to)
        clean_name = rename_to if new_ext else f"{rename_to}{orig_ext}"
    else:
        clean_name = clean_filename(original_name)

    if clean_name != original_name:
        new_path = os.path.join(os.path.dirname(file_path), clean_name)
        try:
            os.rename(file_path, new_path)
            file_path = new_path
        except Exception:
            clean_name = original_name  # rename failed, fall back to original

    # ══════════════════════════════════════════════════════════════════════
    # EXTRACT MODE  (-e flag)
    # ══════════════════════════════════════════════════════════════════════
    if extract_mode:
        extract_dir = os.path.join(
            GLOBAL_STATE["DOWNLOAD_DIR"],
            f"extracted_{gid}"
        )
        os.makedirs(extract_dir, exist_ok=True)

        loop = asyncio.get_running_loop()
        GLOBAL_STATE["ACTIVE"][gid] = {
            "cancel": False, "chat_id": m.chat.id,
            "name": clean_name, "kind": "🗜️ EX",
            "progress": {"done": 0, "total": 1, "speed": 0},
            "start_time": time.time(),
            "user_first": m.from_user.first_name, "user_id": m.from_user.id,
            "mode_tag": mode_tag
        }
        await _ensure_dashboard(app, m.chat.id)
        msg = GLOBAL_STATE["DASHBOARDS"][m.chat.id]["msg"]

        success, extract_msg = await extract_file(
            file_path, extract_dir,
            msg, clean_name, download_index,
            m.from_user.first_name, m.from_user.id, loop,
            progress_gid=gid
        )
        GLOBAL_STATE["ACTIVE"].pop(gid, None)

        # Always delete the original archive after extraction attempt
        if os.path.exists(file_path):
            await asyncio.to_thread(os.remove, file_path)

        if not success:
            # Cleanup empty extract dir on failure
            try:
                await asyncio.to_thread(subprocess.run, ["rm", "-rf", extract_dir], capture_output=True, timeout=15)
            except Exception:
                pass
            return await edit_message_async(
                msg,
                f"❌ **Extraction failed:**\n`{extract_msg}`",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        # Collect all extracted files recursively
        extracted_files = []
        for root, _, files in os.walk(extract_dir):
            for fname in sorted(files):
                extracted_files.append(os.path.join(root, fname))

        if not extracted_files:
            return await edit_message_async(
                msg,
                "⚠️ Extraction succeeded but **no files** were found inside the archive.",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        await edit_message_async(
            msg,
            f"✅ Extracted **{len(extracted_files)}** file(s) from `{clean_name}`.\n"
            f"⬆️ Uploading (up to 4 in parallel)...",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        # ── Prep all file entries first (fast, no waiting) ──────────────────
        upload_jobs = []
        for idx, extracted_path in enumerate(extracted_files, start=1):
            original_extracted_name = os.path.basename(extracted_path)
            extracted_name = clean_filename(original_extracted_name)
            if extracted_name != original_extracted_name:
                new_extracted_path = os.path.join(os.path.dirname(extracted_path), extracted_name)
                try:
                    os.rename(extracted_path, new_extracted_path)
                    extracted_path = new_extracted_path
                except Exception:
                    extracted_name = original_extracted_name
            extracted_size = os.path.getsize(extracted_path)
            sub_gid        = f"{gid}_ex{idx}"

            GLOBAL_STATE["ACTIVE"][sub_gid] = {
                "cancel":      False,
                "file_path":   extracted_path,
                "name":        extracted_name,
                "kind":        "⬆️ UL",
                "progress":    {"done": 0, "total": extracted_size, "speed": 0},
                "last_edit":   0,
                "msg":         msg,           # shared parent msg — no per-file spam
                "extract_dir": extract_dir,   # so cancel can nuke the whole folder
                "base_gid":    gid,           # so cancel can kill all siblings
                "chat_id":     m.chat.id,
                "start_time":  time.time(),
                "user_first":  m.from_user.first_name, "user_id": m.from_user.id
            }
            upload_jobs.append((extracted_path, extracted_name, extracted_size, sub_gid, msg))

        # ── Fire all uploads concurrently — UPLOAD_SEMAPHORE caps it at 4
        # actually running at once; the rest queue automatically. No more
        # waiting on each file one-by-one before the next starts. ──────────
        await asyncio.gather(*[
            upload_file(
                app, file_msg, extracted_path, extracted_name,
                extracted_size, loop, sub_gid,
                download_index, m.from_user.first_name, m.from_user.id
            )
            for extracted_path, extracted_name, extracted_size, sub_gid, file_msg in upload_jobs
        ])

        # Final cleanup: remove extracted folder
        try:
            await asyncio.to_thread(
                subprocess.run, ["rm", "-rf", extract_dir],
                capture_output=True, timeout=30
            )
        except Exception:
            pass

        await edit_message_async(
            msg,
            f"🎉 All **{len(extracted_files)}** extracted file(s) from `{clean_name}` uploaded successfully!",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return  # Done with extract mode

    # ══════════════════════════════════════════════════════════════════════
    # NORMAL UPLOAD (no -e flag)
    # ══════════════════════════════════════════════════════════════════════
    GLOBAL_STATE["ACTIVE"][gid] = {
        "cancel":    False,
        "file_path": file_path,
        "name":      clean_name,
        "kind":      "⬆️ UL",
        "progress":  {"done": 0, "total": file_size, "speed": 0},
        "last_edit": 0,
        "msg":       msg,
        "chat_id":   m.chat.id,
        "start_time": time.time(),
        "user_first": m.from_user.first_name, "user_id": m.from_user.id
    }
    await _ensure_dashboard(app, m.chat.id)
    msg = GLOBAL_STATE["DASHBOARDS"][m.chat.id]["msg"]
    loop = asyncio.get_running_loop()
    asyncio.create_task(
        upload_file(
            app, msg, file_path, clean_name, file_size,
            loop, gid, download_index,
            m.from_user.first_name, m.from_user.id
        )
    )


async def cancel_handler(_, m: Message):
    text = m.text
    # Strip /cancel<index> prefix — task_id is everything after the FIRST underscore
    # e.g. /cancel3_abc123       -> task_id = "abc123"
    # e.g. /cancel3_abc123_ex2   -> task_id = "abc123_ex2"  (extracted upload)
    raw = text.replace("/cancel", "", 1)           # remove /cancel
    task_id = raw.split("_", 1)[1] if "_" in raw else raw

    if not task_id:
        return await reply_message_async(m, "Invalid cancel command.", parse_mode=None)

    if task_id in GLOBAL_STATE["ACTIVE"]:
        task_info = GLOBAL_STATE["ACTIVE"][task_id]
        task_info["cancel"] = True
        
        if "file_path" in task_info:
            # --- UPLOAD CANCELLATION ---
            file_path    = task_info.get("file_path")
            extract_dir  = task_info.get("extract_dir")   # present for extracted uploads
            base_gid     = task_info.get("base_gid")

            # If this is an extracted-upload task, cancel ALL sibling sub-tasks
            # and nuke the entire extracted folder to free disk space
            if extract_dir and base_gid:
                # Cancel every sibling sub_gid that shares the same base_gid
                siblings = [k for k in list(GLOBAL_STATE["ACTIVE"].keys())
                            if k.startswith(f"{base_gid}_ex")]
                for sib_key in siblings:
                    sib = GLOBAL_STATE["ACTIVE"].get(sib_key)
                    if sib:
                        sib["cancel"] = True

                # Nuke the entire extracted folder
                extract_dir_str = str(extract_dir)
                if os.path.exists(extract_dir_str):
                    try:
                        await asyncio.to_thread(
                            subprocess.run,
                            ["rm", "-rf", extract_dir_str],
                            capture_output=True, timeout=30
                        )
                        print(f"Deleted extract dir: {extract_dir_str}")
                    except Exception as e:
                        print(f"Failed to delete extract dir {extract_dir_str}: {e}")

                await reply_message_async(
                    m,
                    f"🛑 Cancelled extracted upload **{task_info['name']}**.\n"
                    "All remaining extracted files and folder deleted.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )

            else:
                # Plain (non-extracted) upload — just delete the single file
                if file_path and os.path.exists(str(file_path)):
                    await asyncio.to_thread(os.remove, str(file_path))
                await reply_message_async(
                    m,
                    f"🛑 Cancelled Upload **{task_info['name']}**.\nFile deleted.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
        
        else:
            # --- DOWNLOAD CANCELLATION ---
            file_path_to_delete = None
            try:
                dl = await asyncio.to_thread(ARIA2_API.get_download, task_id)
                
                if dl and dl.files and dl.files[0] and dl.files[0].path:
                    file_path_to_delete = dl.files[0].path

                await asyncio.to_thread(ARIA2_API.remove, [dl], force=True)
                
            except Exception as e:
                print(f"Aria2 remove failed for GID {task_id}: {e}")
            
            if file_path_to_delete:
                main_file_path = str(file_path_to_delete)
                temp_file_path = main_file_path + '.aria2'
                
                if os.path.exists(main_file_path):
                    await asyncio.to_thread(os.remove, main_file_path)
                    print(f"Manually deleted main file: {main_file_path}")
                
                if os.path.exists(temp_file_path):
                    await asyncio.to_thread(os.remove, temp_file_path)
                    print(f"Manually deleted temp file: {temp_file_path}")
            
            await reply_message_async(m, f"🛑 Cancelled Download GID: **{task_id}**", parse_mode=enums.ParseMode.MARKDOWN)
            
    else:
        await reply_message_async(m, f"Task ID **{task_id}** not found or already complete.", parse_mode=enums.ParseMode.MARKDOWN)


async def status_handler(app, m: Message):
    """/status now spawns a fresh dashboard message (same tree-style, auto-refreshing)."""
    await _ensure_dashboard(app, m.chat.id, force_new=True)


async def status_callback_handler(app, cq: CallbackQuery):
    """Legacy no-op — status now uses the dash_ callback family via dashboard_callback_handler."""
    await cq.answer()


# ================= DASHBOARD (WZML-X style) =================
# One single message per chat, auto-refreshing, paginated, with
# Prev/Next/Refresh/Close buttons — replaces spamming a message per task.

DASH_PAGE_SIZE        = 3
DASH_REFRESH_INTERVAL = 5  # seconds


def _dashboard_tasks(chat_id):
    """Snapshot of all active DL/UL/Extract tasks for this chat (insertion order)."""
    tasks = []
    for gid, info in list(GLOBAL_STATE["ACTIVE"].items()):
        if info.get("chat_id") != chat_id:
            continue

        if "kind" in info:
            # Generic stored-progress entry — covers uploads AND extraction,
            # both push live stats into info["progress"] as they run.
            prog = info.get("progress") or {}
            kind = info["kind"]
            tasks.append({
                "gid": gid, "kind": kind, "name": info.get("name", "?"),
                "done": prog.get("done", 0), "total": prog.get("total", 0),
                "speed": prog.get("speed", 0),
                "status": "Upload" if kind == "⬆️ UL" else "Extract",
                "engine": GLOBAL_STATE.get("ENGINE_NAME", "Pyroblack") if kind == "⬆️ UL" else "pExtract v2",
                "mode_suffix": " (Unzip)" if kind == "🗜️ EX" else "",
                "start_time": info.get("start_time", time.time()),
                "user_first": info.get("user_first", "?"),
                "user_id": info.get("user_id", "?"),
            })
        else:
            # Plain download task — query aria2 live
            try:
                dl = ARIA2_API.get_download(gid)
                if dl.is_complete or dl.is_removed or dl.has_failed:
                    continue
                tasks.append({
                    "gid": gid, "kind": "⬇️ DL", "name": dl.name,
                    "done": dl.completed_length, "total": dl.total_length,
                    "speed": dl.download_speed,
                    "status": "Download",
                    "engine": "Aria2 v1.36.0",
                    "mode_suffix": "",
                    "start_time": info.get("start_time", time.time()),
                    "user_first": info.get("user_first", "?"),
                    "user_id": info.get("user_id", "?"),
                })
            except Exception:
                continue
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
        r = await asyncio.to_thread(
            subprocess.run, ["df", "-BG", "--output=avail,pcent", GLOBAL_STATE["DOWNLOAD_DIR"]],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            lines = r.stdout.strip().split("\n")
            if len(lines) > 1:
                parts = lines[1].split()
                if len(parts) >= 2:
                    free_disk = parts[0].replace("G", "")
                    free_pct  = parts[1].replace("%", "")
    except Exception:
        pass
    uptime_str = time_fmt(time.time() - getattr(app, "start_time", time.time()))
    return cpu, ram, free_disk, free_pct, uptime_str


async def render_dashboard(app, chat_id, page):
    tasks = _dashboard_tasks(chat_id)
    total_tasks = len(tasks)
    total_pages = max(1, (total_tasks + DASH_PAGE_SIZE - 1) // DASH_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * DASH_PAGE_SIZE: page * DASH_PAGE_SIZE + DASH_PAGE_SIZE]

    dl_speed_total = sum(t["speed"] for t in tasks if t["kind"] == "⬇️ DL")
    ul_speed_total = sum(t["speed"] for t in tasks if t["kind"] == "⬆️ UL")

    lines = []
    if not page_tasks:
        lines.append("✅ No active tasks.")
    else:
        for t in page_tasks:
            name = _html.escape(t["name"])
            elapsed = time_fmt(int(time.time() - t["start_time"]))
            eta_sec = (t["total"] - t["done"]) / t["speed"] if t["speed"] > 0 else 0
            lines.append(f"<b>{name}</b>")
            lines.append(f"   {progress_bar(t['done'], t['total'])}")
            lines.append(f"├ <b>Processed:</b> {format_size(t['done'])} of {format_size(t['total'])}")
            lines.append(f"├ <b>Status:</b> {t['status']} | <b>ETA:</b> {time_fmt(eta_sec)}")
            lines.append(f"├ <b>Speed:</b> {format_speed(t['speed'])} | <b>Elapsed:</b> {elapsed}")
            lines.append(f"├ <b>Engine:</b> {t['engine']}")
            lines.append(f"├ <b>Mode:</b>  #Leech{t['mode_suffix']} | #Aria2")
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
    - force_new=False (default, used for mid-task refreshes): reuse the
      currently running dashboard if there is one.
    - force_new=True (used when a brand new /l, /ql, or /status comes in):
      freeze the old dashboard message in place (stop refreshing it, strip
      its buttons, but NEVER delete it) and pop a fresh one under the new command.
    """
    existing = GLOBAL_STATE["DASHBOARDS"].get(chat_id)

    if existing and existing.get("running"):
        if not force_new:
            return
        existing["running"] = False  # old loop sees gen mismatch next tick and stops itself
        try:
            await existing["msg"].edit_reply_markup(reply_markup=None)  # remove buttons, leave text as final state
        except Exception:
            pass

    text, keyboard, page = await render_dashboard(app, chat_id, 0)
    msg = await app.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
    gen = time.time()
    GLOBAL_STATE["DASHBOARDS"][chat_id] = {"msg": msg, "page": 0, "running": True, "gen": gen}
    asyncio.create_task(_dashboard_loop(app, chat_id, gen))


async def _dashboard_loop(app, chat_id, gen):
    """Background auto-refresh — stops itself if superseded by a newer dashboard (gen mismatch)."""
    while True:
        entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
        if not entry or not entry.get("running") or entry.get("gen") != gen:
            return
        text, keyboard, page = await render_dashboard(app, chat_id, entry["page"])
        entry["page"] = page
        await edit_message_async(entry["msg"], text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        await asyncio.sleep(DASH_REFRESH_INTERVAL)


async def dashboard_callback_handler(app, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
    if not entry:
        return await cq.answer("Dashboard expired — start a new /l or /ql.", show_alert=True)

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
    # dash_refresh: page unchanged, just re-render

    text, keyboard, page = await render_dashboard(app, chat_id, entry["page"])
    entry["page"] = page
    try:
        await cq.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        await cq.answer()
    except FloodWait as e:
        await cq.answer(f"Flood control: {e.value}s", show_alert=True)
    except Exception:
        await cq.answer()


async def stats_handler(app, m: Message):
    # ── 1. System Stats (Subprocess) ──────────────────────────────────────
    cpu_percent = "N/A"
    ram_usage   = "N/A"
    disk_info   = "Disk info unavailable"

    # Check if py7zr is installed
    try:
        import py7zr
        sevenzip_status = f"✅ py7zr {py7zr.version.__version__} installed"
    except ImportError:
        sevenzip_status = "❌ Not found (pip install py7zr)"
    except Exception:
        sevenzip_status = "⚠️ Unknown"

    # Get CPU Load
    try:
        cpu_result = subprocess.run(
            ["sh", "-c", "top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}'"],
            capture_output=True, text=True, timeout=5, check=True
        )
        if cpu_result.returncode == 0:
            cpu_percent = f"{float(cpu_result.stdout.strip()):.1f}%"
    except Exception:
        pass

    # Get RAM Usage
    try:
        ram_result = subprocess.run(
            ["sh", "-c", "free -m | awk 'NR==2{printf \"%.1f%%\", $3*100/$2 }'"],
            capture_output=True, text=True, timeout=5, check=True
        )
        if ram_result.returncode == 0:
            ram_usage = ram_result.stdout.strip()
    except Exception:
        pass

    # Get Disk Usage
    try:
        df_result = subprocess.run(
            ["df", "-h", "--output=size,avail,pcent", GLOBAL_STATE["DOWNLOAD_DIR"]],
            capture_output=True, text=True, timeout=5, check=True
        )
        if df_result.returncode == 0:
            df_lines = df_result.stdout.strip().split('\n')
            if len(df_lines) > 1:
                df_data = df_lines[1].split()
                if len(df_data) >= 3:
                    total_disk, free_disk, disk_percent = df_data[0], df_data[1], df_data[2]
                    disk_info = f"F: {free_disk} | T: {total_disk} [{disk_percent}]"
    except Exception:
        pass

    # ── 2. Bot State & Time ────────────────────────────────────────────────
    uptime_sec    = time.time() - app.start_time
    uptime_str    = time_fmt(uptime_sec)
    total_dl_str  = time_fmt(GLOBAL_STATE["TOTAL_DOWNLOAD_TIME"])
    total_ul_str  = time_fmt(GLOBAL_STATE["TOTAL_UPLOAD_TIME"])
    active_download = GLOBAL_STATE["DOWNLOAD_COUNT"]
    active_upload   = GLOBAL_STATE["UPLOAD_COUNT"]

    # ── 3. Build Message ───────────────────────────────────────────────────
    stats_text = (
        "🤖 **Bot Status Report**\n"
        "--- System Metrics ---\n"
        f"┠ CPU Load: **{cpu_percent}**\n"
        f"┠ RAM Usage: **{ram_usage}**\n"
        f"┖ Disk: **{disk_info}**\n"
        "\n"
        "--- Tools ---\n"
        f"┖ 7zip (p7zip): **{sevenzip_status}**\n"
        "\n"
        "--- Transfer Activity ---\n"
        f"┠ Active DLs: **{active_download}**\n"
        f"┖ Active ULs: **{active_upload}**\n"
        "\n"
        "--- Cumulative Stats ---\n"
        f"┠ Total DL Time: **{total_dl_str}**\n"
        f"┠ Total UL Time: **{total_ul_str}**\n"
        f"┖ Bot Uptime: **{uptime_str}**"
    )

    await reply_message_async(m, stats_text, parse_mode=enums.ParseMode.MARKDOWN)


# ================= REGISTRATION FUNCTION =================

def register_handlers(app, aria2_api, state_vars):
    """
    Registers all command handlers to the Pyrogram client instance and sets 
    the necessary global variables in this module.
    """
    global ARIA2_API, GLOBAL_STATE

    ARIA2_API    = aria2_api
    GLOBAL_STATE = state_vars
    
    # --- Handlers that only need default args (client, message/callback) ---
    app.on_message(filters.command("start"))(start_handler)
    app.on_message(filters.regex(r"^/cancel"))(cancel_handler)
    app.on_callback_query(filters.regex("^status_"))(status_callback_handler)
    app.on_callback_query(filters.regex("^dash_"))(dashboard_callback_handler)

    # --- Handlers that take (app, message) args (using wrappers) ---

    @app.on_message(filters.command(["l", "leech"]))
    async def leech_wrapper(client, message):
        await leech_handler(client, message)

    @app.on_message(filters.command("ql"))
    async def ql_wrapper(client, message):
        await ql_handler(client, message)

    @app.on_message(filters.command("status"))
    async def status_wrapper(client, message):
        await status_handler(client, message)

    @app.on_message(filters.command("stats"))
    async def stats_wrapper(client, message):
        await stats_handler(client, message)
