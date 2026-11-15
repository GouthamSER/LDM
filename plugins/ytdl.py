# plugins/ytdl.py
"""
yt-dlp engine (ported from Leechv2's yt_dlp_download.py / ytdlp.py, trimmed).

  /y <url>                 -> info fetch, quality picker (buttons), download, upload
  /y <url> -q <format>     -> skip the picker (yt-dlp format string, or: best | audio | mp3 | 720 ...)
  /y <url> -n <name>       -> rename (single video)
  /y <url> -np             -> single video only, even if the URL has a playlist

This module only holds engine plumbing + the picker. The task pipeline
(dashboard, rename rules, upload, cleanup) is shared and lives in commands.py.
"""

import ast
import asyncio
import os
import shutil
from secrets import token_hex

import config

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
    YTDL_AVAILABLE = True
except ImportError:
    YTDL_AVAILABLE = False

# Exact Leechv2 output templates (title + season/episode + height/fps/bitrate)
OUTTMPL_ITEM = (
    "%(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d"
    "%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s"
    "%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s"
)

JUNK_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".json", ".part", ".ytdl", ".temp")
# callback registry: key -> {"event", "qual", "uid", "formats"}
PENDING = {}


def version():
    return yt_dlp.version.__version__ if YTDL_AVAILABLE else "?"


# ───────────────────────── options ─────────────────────────

def base_opts(cookiefile=None):
    ffmpeg = shutil.which("ffmpeg")
    opts = {
        "usenetrc": True,
        "allow_multiple_video_streams": True,
        "allow_multiple_audio_streams": True,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "allow_playlist_files": True,
        "overwrites": True,
        "trim_file_name": 220,
        "fragment_retries": 10,
        "retries": 10,
        "retry_sleep_functions": {
            "http": lambda n: 3, "fragment": lambda n: 3,
            "file_access": lambda n: 3, "extractor": lambda n: 3,
        },
    }
    if ffmpeg:
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    if config.YTDL_OPTIONS:
        try:
            extra = ast.literal_eval(config.YTDL_OPTIONS)
            if isinstance(extra, dict):
                opts.update(extra)
        except Exception as e:
            print(f"⚠️ YTDL_OPTIONS ignored (not a dict literal): {e}")
    return opts


def extract_info(url, no_playlist=False, cookiefile=None):
    """Blocking. Flat playlist extraction keeps big playlists fast."""
    opts = base_opts(cookiefile)
    opts["extract_flat"] = "in_playlist"
    if no_playlist:
        opts["noplaylist"] = True
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError("yt-dlp returned no info")
    return info


# ───────────────────────── quality mapping ─────────────────────────

def video_fmt(h):
    return f"bv*[height<=?{h}][ext=mp4]+ba[ext=m4a]/b[height<=?{h}]/bv*[height<=?{h}]+ba/b[height<=?{h}]/bv*+ba/b"


def parse_quality(q):
    """User text -> (format_string, audio_codec|None, audio_quality|None). None if not understood."""
    q = (q or "").strip().lower()
    if q in ("best", "bv*+ba/b", ""):
        return "bv*+ba/b", None, None
    if q in ("audio", "ba", "ba/b"):
        return "ba/b", None, None
    if q in ("mp3", "m4a", "flac", "opus", "wav"):
        return "ba/b", q, "192" if q == "mp3" else "0"
    if q.rstrip("p").isdigit():
        return video_fmt(q.rstrip("p")), None, None
    # anything else = a raw yt-dlp format expression
    return q, None, None


def picker_buttons(info, key):
    """Build the quality keyboard from the heights actually available."""
    heights = set()
    sizes = {}
    for f in info.get("formats") or []:
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none"):
            heights.add(h)
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            sizes[h] = max(sizes.get(h, 0), sz)
    std = [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]
    avail = sorted({min(std, key=lambda s: abs(s - h)) for h in heights}) if heights else [360, 480, 720, 1080]
    rows = []
    row = []
    for h in avail:
        row.append((f"{h}p", f"ytq {key} v{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⭐ Best Video", f"ytq {key} best"), ("🎧 Best Audio", f"ytq {key} audio")])
    rows.append([("MP3 192k", f"ytq {key} mp3_192"), ("MP3 320k", f"ytq {key} mp3_320"),
                 ("M4A", f"ytq {key} m4a")])
    rows.append([("❌ Cancel", f"ytq {key} cancel")])
    return rows


def decode_choice(code):
    """Callback code -> (format, codec, quality) or None for cancel."""
    if code == "cancel":
        return None
    if code == "best":
        return parse_quality("best")
    if code == "audio":
        return parse_quality("audio")
    if code == "m4a":
        return "ba/b", "m4a", "192"
    if code.startswith("mp3_"):
        return "ba/b", "mp3", code.split("_")[1]
    if code.startswith("v") and code[1:].isdigit():
        return video_fmt(code[1:]), None, None
    return parse_quality(code)


def new_pending(uid):
    key = token_hex(3)
    PENDING[key] = {"event": asyncio.Event(), "choice": "unset", "uid": uid}
    return key


# ───────────────────────── download ─────────────────────────

def download_blocking(url, outdir, fmt, codec, acq, is_playlist, entry, no_playlist, cookiefile=None):
    """Runs in a worker thread. On "Requested format is not available" retries with looser
    formats (then without cookies: stale/limited-account cookies can hide formats)."""
    attempts = [(fmt, cookiefile), ("bv*+ba/b/best", cookiefile)]
    if cookiefile:
        attempts.append(("bv*+ba/b/best", None))
    last = None
    for f, ck in attempts:
        try:
            return _download_once(url, outdir, f, codec, acq, is_playlist, entry, no_playlist, ck)
        except Exception as e:
            last = e
            if "format is not available" not in str(e).lower() or entry.get("cancel"):
                raise
            print(f"⚠️ yt-dlp format '{f}' (cookies={bool(ck)}) unavailable, retrying looser")
    raise last


def _download_once(url, outdir, fmt, codec, acq, is_playlist, entry, no_playlist, cookiefile=None):
    state = {"base": 0, "last": 0}

    def hook(d):
        if entry.get("cancel"):
            raise ValueError("Cancelling...")
        st = d.get("status")
        if st == "downloading":
            cur = d.get("downloaded_bytes") or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            base = state["base"]
            entry["status"] = "Download"
            entry["progress"] = {
                "done": base + cur,
                "total": max(base + tot, base + cur),
                "speed": d.get("speed") or 0,
            }
            fn = d.get("info_dict", {}).get("title")
            if fn and not entry.get("name_locked"):
                entry["name"] = fn
            if d.get("eta") is not None:
                entry["eta"] = d["eta"]
        elif st == "finished":
            state["base"] += d.get("total_bytes") or d.get("downloaded_bytes") or 0

    def pp_hook(d):
        if d.get("status") == "started":
            entry["status"] = "Processing"

    opts = base_opts(cookiefile)
    opts.update({
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "format": fmt,
        "outtmpl": {"default": os.path.join(outdir, OUTTMPL_ITEM),
                    "thumbnail": os.path.join(outdir, ".thumbs", OUTTMPL_ITEM)},
    })
    pps = [{"key": "FFmpegMetadata", "add_chapters": True, "add_infojson": "if_exists", "add_metadata": True}]
    if codec:
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": acq or "192"})
    else:
        opts["merge_output_format"] = "mp4"
    if codec in ("mp3", "m4a", "flac", "opus"):
        # cover art for audio files. Videos skip this: embedding fails on webm and the
        # upload step already makes a Telegram thumbnail.
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    opts["postprocessors"] = pps
    if is_playlist:
        opts["ignoreerrors"] = True
    if no_playlist:
        opts["noplaylist"] = True

    with YoutubeDL(opts) as ydl:
        ydl.download([url])


def collect_files(outdir):
    """Media files produced by yt-dlp (thumbnails / sidecars dropped)."""
    out = []
    for root, dirs, files in os.walk(outdir):
        dirs[:] = [d for d in dirs if d != ".thumbs"]
        for f in sorted(files):
            if f.lower().endswith(JUNK_EXTS):
                continue
            p = os.path.join(root, f)
            if os.path.getsize(p) > 0:
                out.append(p)
    return out
