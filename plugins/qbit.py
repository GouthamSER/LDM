# plugins/qbit.py
"""
qBittorrent engine for the bot (WebUI API via aioqbt, qBittorrent 5.x).

Only talks to the local qbittorrent-nox started by start.sh. Everything here
is engine plumbing: add / find / inspect / remove. The progress loop and the
upload pipeline live in plugins/commands.py so aria2 and qBit share them.
"""

import asyncio
import base64
import hashlib
import os
import re

import config

try:
    from aioqbt.client import create_client
    from aioqbt.api import AddFormBuilder
    from aioqbt.exc import AddTorrentError
    QBIT_AVAILABLE = True
except ImportError:  # aioqbt missing -> /qb replies with a clear error
    QBIT_AVAILABLE = False
    AddTorrentError = Exception  # type: ignore

_client = None
_client_lock = None
_version = "?"

# libtorrent tuned for small boxes (Koyeb 512MB etc): POSIX disk IO instead of
# mmap (mmap pages count against the cgroup limit), tiny caches, few peers.
LOW_RAM_PREFS = {
    "disk_io_type": 2,
    "disk_cache": 32,
    "async_io_threads": 2,
    "file_pool_size": 20,
    "checking_memory_use": 32,
    "max_connec": 100,
    "max_connec_per_torrent": 40,
    "max_uploads": 4,
    "max_uploads_per_torrent": 2,
    "send_buffer_watermark": 500,
    "send_buffer_low_watermark": 10,
    "send_buffer_watermark_factor": 50,
    "max_active_downloads": 5,
    "max_active_torrents": 10,
    "max_active_checking_torrents": 1,
    "dht": True,
    "pex": True,
    "lsd": True,
}

EXTRA_TRACKERS = [
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
    "udp://opentracker.i2p.rocks:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.gbitt.info:443/announce",
    "https://tracker.tamersunion.org:443/announce",
]


# ───────────────────────── client ─────────────────────────

async def get_client():
    """Lazy singleton. Re-created if the session was closed."""
    global _client, _client_lock, _version
    if not QBIT_AVAILABLE:
        raise RuntimeError("aioqbt not installed (pip install aioqbt)")
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    async with _client_lock:
        if _client is not None and not _client.closed:
            return _client
        url = f"http://{config.QBIT_HOST}:{config.QBIT_PORT}/api/v2/"
        user = config.QBIT_USERNAME or None
        pw = config.QBIT_PASSWORD or None
        _client = await create_client(url, user, pw)
        try:
            _version = (await _client.app.version()).lstrip("v")
        except Exception:
            _version = "?"
        await _apply_prefs(_client)
        return _client


def version():
    return _version


async def _apply_prefs(c):
    """Best effort, runs once per client. Unknown keys are ignored by qBit."""
    prefs = dict(LOW_RAM_PREFS)
    prefs["save_path"] = os.path.abspath(config.DOWNLOAD_DIR)
    prefs["add_trackers_enabled"] = True
    prefs["add_trackers"] = "\n".join(EXTRA_TRACKERS)
    try:
        await c.app.set_preferences(prefs)
    except Exception as e:
        print(f"⚠️ qBit prefs not applied: {e}")


async def ping():
    try:
        await get_client()
        return True
    except Exception:
        return False


# ───────────────────────── hash helpers ─────────────────────────

def _bdecode(data: bytes, i: int = 0):
    """Tiny bencode decoder -> (value, next_index). Dict values keep raw span
    of the 'info' key in `_bdecode.info_span` so we can sha1 it exactly."""
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            start = i
            v, i = _bdecode(data, i)
            if k == b"info":
                _bdecode.info_span = (start, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bad bencode")


_bdecode.info_span = None


def torrent_hash(data: bytes):
    """v1 info-hash (hex) of .torrent bytes, or None if unreadable."""
    try:
        _bdecode.info_span = None
        top, _ = _bdecode(data)
        if not isinstance(top, dict) or not _bdecode.info_span:
            return None
        s, e = _bdecode.info_span
        return hashlib.sha1(data[s:e]).hexdigest()
    except Exception:
        return None


def torrent_name(data: bytes):
    try:
        top, _ = _bdecode(data)
        return top[b"info"][b"name"].decode("utf-8", "replace")
    except Exception:
        return None


def magnet_hash(uri: str):
    m = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", uri)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 40:
        return h.lower()
    if len(h) == 32:
        try:
            return base64.b32decode(h.upper()).hex()
        except Exception:
            return None
    return None


# ───────────────────────── operations ─────────────────────────

async def _find(client, h):
    res = await client.torrents.info(hashes=[h])
    return res[0] if res else None


async def add(source, tag, save_path=None):
    """
    Add a magnet (str) or .torrent (bytes). Returns the info-hash (hex).
    On 409 (already in qBit, e.g. leftover from a crashed run) the old entry
    and its files are wiped and the add is retried once.
    """
    client = await get_client()
    save_path = save_path or os.path.abspath(config.DOWNLOAD_DIR)
    is_magnet = isinstance(source, str)
    want_hash = magnet_hash(source) if is_magnet else torrent_hash(source)

    def build():
        b = AddFormBuilder.with_client(client).savepath(save_path).tags([tag])
        if is_magnet:
            return b.include_url(source).build()
        return b.include_file(source, "task.torrent").build()

    try:
        await client.torrents.add(build())
    except AddTorrentError:
        if not want_hash:
            raise
        try:
            await client.torrents.delete([want_hash], True)
        except Exception:
            pass
        await asyncio.sleep(2)
        await client.torrents.add(build())

    # Find it: by hash when we could compute one, else by our unique tag.
    for _ in range(30):
        if want_hash:
            t = await _find(client, want_hash)
        else:
            lst = await client.torrents.info(tag=tag)
            t = lst[0] if lst else None
        if t:
            return t.hash
        await asyncio.sleep(1)
    raise RuntimeError("qBittorrent accepted the torrent but it never showed up")


async def info(h):
    client = await get_client()
    return await _find(client, h)


async def files(h):
    client = await get_client()
    return await client.torrents.files(h)


async def remove(h, delete_files=True):
    client = await get_client()
    await client.torrents.delete([h], delete_files)


def state_text(state):
    s = str(getattr(state, "value", state))
    return {
        "metaDL": "Fetching metadata",
        "downloading": "Download",
        "stalledDL": "Stalled",
        "forcedDL": "Download",
        "queuedDL": "Queued",
        "checkingDL": "Checking",
        "checkingResumeData": "Checking",
        "allocating": "Allocating",
        "moving": "Moving",
        "stoppedDL": "Stopped",
        "pausedDL": "Stopped",
        "error": "Error",
        "missingFiles": "Missing files",
    }.get(s, s)


def is_done(t):
    """Fully downloaded (qBit flips to an *UP / stopped state when complete)."""
    s = str(getattr(t.state, "value", t.state))
    return t.progress >= 1.0 or s in (
        "uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP", "stoppedUP", "pausedUP"
    )


def is_error(t):
    return str(getattr(t.state, "value", t.state)) in ("error", "missingFiles")
