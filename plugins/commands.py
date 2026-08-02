# plugins/commands.py

import os
import time
import asyncio
import subprocess
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

def progress_bar(done, total, size=12):
    # WZML-X classic style: solid block for filled, light shade for empty
    FILLED = "█"
    EMPTY = "░"
    if total == 0:
        return f"[{EMPTY * size}] 0.00%"
    percent = min(100.0, (done / total) * 100)
    filled_count = int(percent / 100 * size)
    if percent > 0 and filled_count == 0 and size > 0: filled_count = 1
    if filled_count > size: filled_count = size
    empty_count = size - filled_count
    bar = FILLED * filled_count + EMPTY * empty_count
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
    msg, name, download_index, user_first, user_id, loop
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
        """Push a live extraction progress message (called from worker thread)."""
        elapsed = time.time() - start_time
        speed   = done / elapsed if elapsed > 0 else 0
        eta     = (total - done) / speed if speed > 0 else 0
        content = (
            f"📂 Name: {name}\n"
            f"{progress_bar(done, total)}\n"
            f"📦 Processed: {format_size(done)} of {format_size(total)}\n"
            f"⚙️ Status: Extracting | Engine: py7zr\n"
            f"🚀 Speed: {format_speed(speed)} | ⏳ ETA: {time_fmt(eta)}\n"
            f"⏱ Elapsed: {time_fmt(elapsed)}\n"
            f"🎯 Mode: #Extract | #7zip\n"
            f"👤 User: {user_first} | ID: {user_id}"
        )
        coro = edit_message_async(msg, content, enums.ParseMode.MARKDOWN)
        asyncio.run_coroutine_threadsafe(coro, loop)

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

    current_time = time.time()
    if current_time - GLOBAL_STATE["ACTIVE"].get(gid, {}).get("last_edit", 0) < 3:
        return

    eta = (total - current) / speed if speed > 0 else 0
    eta_str = time_fmt(eta)
    elapsed_str = time_fmt(elapsed)
    progress_bar_output = progress_bar(current, total)
    
    new_content = (
        f"📂 Name: {name}\n"
        f"{progress_bar_output}\n"
        f"📦 Processed: {format_size(current)} of {format_size(total)}\n"
        f"⚙️ Status: Uploading | Engine: {GLOBAL_STATE.get('ENGINE_NAME', 'Pyroblack')}\n"
        f"🚀 Speed: {format_speed(speed)} | ⏳ ETA: {eta_str}\n"
        f"⏱ Elapsed: {elapsed_str}\n"
        f"🎯 Mode: #Leech\n"
        f"👤 User: {user_first} | ID: {user_id}\n"
        f"🆔 /cancel{download_index}_{gid}"
    )
    
    msg = GLOBAL_STATE["ACTIVE"][gid]["msg"]
    try:
        coro = edit_message_async(msg, new_content, parse_mode)
        asyncio.run_coroutine_threadsafe(coro, loop)
        GLOBAL_STATE["ACTIVE"][gid]["last_edit"] = current_time
    except:
        pass

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
        "┠ Live auto-refreshing dashboard: `/dashboard` (or `/db`)\n"
        "┠ To view system stats: `/stats`\n"
        "┖ To cancel an active task: `/cancel<index>_<gid>`\n\n"
        "**⚡ Uploads:** up to 4 run in parallel — no waiting on slow files.\n\n"
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

    msg = await reply_message_async(
        m,
        f"🚀 Starting download {download_index}\nGID: `{gid}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    GLOBAL_STATE["ACTIVE"][gid] = {"cancel": False, "start_time": start_time, "chat_id": m.chat.id}
    GLOBAL_STATE["DOWNLOAD_COUNT"] += 1

    # ── Download progress loop ─────────────────────────────────────────────
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
            GLOBAL_STATE["ACTIVE"][gid] = {"cancel": False, "start_time": start_time, "chat_id": m.chat.id}
            continue

        done  = dl.completed_length
        total = dl.total_length
        speed = dl.download_speed
        eta_sec = dl.eta.total_seconds() if isinstance(dl.eta, timedelta) else dl.eta

        eta_str     = time_fmt(eta_sec)
        elapsed_str = time_fmt(int(time.time() - start_time))

        if not (dl.is_removed or dl.has_failed):
            try:
                await edit_message_async(
                    msg,
                    f"📂 Name: {dl.name}\n"
                    f"{progress_bar(done, total)}\n"
                    f"📦 Processed: {format_size(done)} of {format_size(total)}\n"
                    f"⚙️ Status: Downloading | Engine: Aria2 v1.36.0\n"
                    f"🚀 Speed: {format_speed(speed)} | ⏳ ETA: {eta_str}\n"
                    f"⏱ Elapsed: {elapsed_str}\n"
                    f"🎯 Mode: {mode_tag}\n"
                    f"🌱 Seeders: {getattr(dl, 'num_seeders', 'N/A')} | Leechers: N/A\n"
                    f"👤 User: {m.from_user.first_name} | ID: {m.from_user.id}\n"
                    f"🆔 /cancel{download_index}_{gid}",
                    parse_mode=None
                )
            except Exception:
                pass

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
        await edit_message_async(
            msg,
            f"📂 Name: {clean_name}\n"
            f"{progress_bar(0, 1)}\n"
            f"📦 Processed: 0B of ?\n"
            f"⚙️ Status: Extracting | Engine: py7zr\n"
            f"🚀 Speed: -- | ⏳ ETA: --\n"
            f"⏱ Elapsed: 0s\n"
            f"🎯 Mode: #Extract | #7zip\n"
            f"👤 User: {m.from_user.first_name} | ID: {m.from_user.id}",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        success, extract_msg = await extract_file(
            file_path, extract_dir,
            msg, clean_name, download_index,
            m.from_user.first_name, m.from_user.id, loop
        )

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

            file_msg = await reply_message_async(
                m,
                f"⬆️ Queued **{idx}/{len(extracted_files)}**: `{extracted_name}`\n"
                f"Size: {format_size(extracted_size)}",
                parse_mode=enums.ParseMode.MARKDOWN
            )

            GLOBAL_STATE["ACTIVE"][sub_gid] = {
                "cancel":      False,
                "file_path":   extracted_path,
                "name":        extracted_name,
                "last_edit":   0,
                "msg":         file_msg,
                "extract_dir": extract_dir,   # so cancel can nuke the whole folder
                "base_gid":    gid,           # so cancel can kill all siblings
                "chat_id":     m.chat.id
            }
            upload_jobs.append((extracted_path, extracted_name, extracted_size, sub_gid, file_msg))

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
    await edit_message_async(
        msg,
        f"✅ Download complete! Starting upload of **{clean_name}**\n"
        f"To cancel upload, use `/cancel{download_index}_{gid}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    GLOBAL_STATE["ACTIVE"][gid] = {
        "cancel":    False,
        "file_path": file_path,
        "name":      clean_name,
        "last_edit": 0,
        "msg":       msg,
        "chat_id":   m.chat.id
    }
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
    """Handles the /status command, showing all active tasks."""
    status_text = await asyncio.to_thread(get_all_active_status, app)
    keyboard = get_status_keyboard()
    await reply_message_async(m, status_text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=keyboard)


async def status_callback_handler(app, cq: CallbackQuery):
    """Handles the callback query from the Refresh button."""
    if cq.data == "status_refresh":
        new_status_text = await asyncio.to_thread(get_all_active_status, app)
        keyboard = get_status_keyboard()
        
        try:
            await cq.edit_message_text(new_status_text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=keyboard)
            await cq.answer("Status refreshed successfully!")
        except FloodWait as e:
            await cq.answer(f"Flood control: Try again in {e.value}s.", show_alert=True)
        except Exception:
            await cq.answer("Status message could not be refreshed (too old or unchanged).", show_alert=False)


# ================= DASHBOARD (WZML-X style) =================
# One single message per chat, auto-refreshing, paginated, with
# Prev/Next/Refresh/Close buttons — replaces spamming a message per task.

DASH_PAGE_SIZE        = 3
DASH_REFRESH_INTERVAL = 5  # seconds


def _dashboard_tasks(chat_id):
    """Snapshot of all active DL/UL tasks for this chat, newest first excluded (insertion order)."""
    tasks = []
    for gid, info in list(GLOBAL_STATE["ACTIVE"].items()):
        if info.get("chat_id") != chat_id:
            continue
        if "file_path" in info:
            # Upload task — live stats pushed by upload_progress()
            prog = info.get("progress") or {}
            tasks.append({
                "gid": gid, "kind": "⬆️ UL", "name": info.get("name", "?"),
                "done": prog.get("done", 0), "total": prog.get("total", 0),
                "speed": prog.get("speed", 0)
            })
        else:
            # Download task — query aria2 live
            try:
                dl = ARIA2_API.get_download(gid)
                if dl.is_complete or dl.is_removed or dl.has_failed:
                    continue
                tasks.append({
                    "gid": gid, "kind": "⬇️ DL", "name": dl.name,
                    "done": dl.completed_length, "total": dl.total_length,
                    "speed": dl.download_speed
                })
            except Exception:
                continue
    return tasks


def render_dashboard(chat_id, page):
    tasks = _dashboard_tasks(chat_id)
    total_tasks = len(tasks)
    total_pages = max(1, (total_tasks + DASH_PAGE_SIZE - 1) // DASH_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * DASH_PAGE_SIZE: page * DASH_PAGE_SIZE + DASH_PAGE_SIZE]

    lines = ["📊 **Dashboard**", "─" * 22]
    if not page_tasks:
        lines.append("✅ No active tasks.")
    else:
        for t in page_tasks:
            lines.append(f"{t['kind']} **{t['name']}**")
            lines.append(f"  {progress_bar(t['done'], t['total'])}")
            lines.append(
                f"  {format_size(t['done'])}/{format_size(t['total'])} • "
                f"{format_speed(t['speed'])} • `/cancel_{t['gid']}`"
            )
    lines.append("─" * 22)
    lines.append(
        f"Page {page + 1}/{total_pages} • Tasks: {total_tasks} • "
        f"DL: {GLOBAL_STATE.get('DOWNLOAD_COUNT', 0)} UL: {GLOBAL_STATE.get('UPLOAD_COUNT', 0)}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Prev", callback_data="dash_prev"),
            InlineKeyboardButton("🔄 Refresh", callback_data="dash_refresh"),
            InlineKeyboardButton("Next ➡️", callback_data="dash_next"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="dash_close")]
    ])
    return "\n".join(lines), keyboard, page


async def dashboard_handler(app, m: Message):
    """/dashboard or /db — opens (or reuses) the single auto-refreshing dashboard for this chat."""
    chat_id = m.chat.id
    existing = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
    if existing and existing.get("running"):
        return await reply_message_async(
            m, "📊 Dashboard already running above — use its buttons to navigate.", parse_mode=None
        )

    text, keyboard, page = render_dashboard(chat_id, 0)
    msg = await reply_message_async(m, text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=keyboard)
    GLOBAL_STATE["DASHBOARDS"][chat_id] = {"msg": msg, "page": 0, "running": True}
    asyncio.create_task(_dashboard_loop(app, chat_id))


async def _dashboard_loop(app, chat_id):
    """Background auto-refresh — edits the single dashboard message every few seconds."""
    while True:
        entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
        if not entry or not entry.get("running"):
            return
        text, keyboard, page = render_dashboard(chat_id, entry["page"])
        entry["page"] = page
        await edit_message_async(entry["msg"], text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=keyboard)
        await asyncio.sleep(DASH_REFRESH_INTERVAL)


async def dashboard_callback_handler(app, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    entry = GLOBAL_STATE["DASHBOARDS"].get(chat_id)
    if not entry:
        return await cq.answer("Dashboard expired — run /dashboard again.", show_alert=True)

    if cq.data == "dash_close":
        entry["running"] = False
        GLOBAL_STATE["DASHBOARDS"].pop(chat_id, None)
        try:
            await cq.edit_message_text("📊 Dashboard closed. Run /dashboard to reopen.")
        except Exception:
            pass
        return await cq.answer("Closed.")

    if cq.data == "dash_prev":
        entry["page"] = max(0, entry["page"] - 1)
    elif cq.data == "dash_next":
        entry["page"] += 1
    # dash_refresh: page unchanged, just re-render

    text, keyboard, page = render_dashboard(chat_id, entry["page"])
    entry["page"] = page
    try:
        await cq.edit_message_text(text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=keyboard)
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

    @app.on_message(filters.command(["dashboard", "db"]))
    async def dashboard_wrapper(client, message):
        await dashboard_handler(client, message)

    @app.on_message(filters.command("stats"))
    async def stats_wrapper(client, message):
        await stats_handler(client, message)
