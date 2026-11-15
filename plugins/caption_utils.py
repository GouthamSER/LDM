# plugins/caption_utils.py
"""
Leech caption builder, ported from Leechv2 (telegram_uploader._prepare_file + media_utils.get_media_info).

Template placeholders (case-insensitive): {filename} {size} {duration} {quality} {languages}
{subtitles} {md5_hash}. HTML is allowed.
Escapes:  \\|  \\{  \\}  \\s(space).
Extra parts after `|` are replacements applied to the final caption: `|old:new[:count]`.
Video-only placeholders on a non-video file: those lines are dropped (like Leechv2).
"""

import asyncio
import hashlib
import html as _html
import json
import re
import shutil

try:
    from langcodes import Language
except Exception:   # optional; falls back to a small table
    Language = None

VIDEO_PH = ("{quality}", "{languages}", "{duration}", "{subtitles}")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
              ".3gp", ".ogv", ".ts", ".m2ts", ".vob", ".divx", ".asf", ".rm", ".rmvb", ".f4v")
VALID_FIELDS = ("filename", "size", "duration", "quality", "languages", "subtitles", "md5_hash")

_FALLBACK_LANG = {
    "eng": "English", "hin": "Hindi", "tam": "Tamil", "tel": "Telugu", "mal": "Malayalam",
    "kan": "Kannada", "jpn": "Japanese", "kor": "Korean", "spa": "Spanish", "fre": "French",
    "fra": "French", "ger": "German", "deu": "German", "chi": "Chinese", "zho": "Chinese",
    "rus": "Russian", "ara": "Arabic", "ita": "Italian", "por": "Portuguese", "tur": "Turkish",
    "ben": "Bengali", "mar": "Marathi", "pan": "Punjabi", "urd": "Urdu", "tha": "Thai",
}


def _lang_name(code):
    if Language is not None:
        try:
            return Language.get(code).display_name()
        except Exception:
            pass
    return _FALLBACK_LANG.get(code.lower(), code)


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def human_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if m else "") + f"{s}s"


def is_video_file(path):
    return path.lower().endswith(VIDEO_EXTS)


async def get_media_info(path):
    """-> (duration_sec, quality, languages, subtitles). Same output as Leechv2's get_media_info(extra_info=True)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0, "", "", ""
    try:
        p = await asyncio.create_subprocess_exec(
            ffprobe, "-hide_banner", "-loglevel", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 40)
        data = json.loads(out)
    except Exception:
        return 0, "", "", ""
    duration = round(float((data.get("format") or {}).get("duration", 0) or 0))
    qual, langs, subs = "N/A", [], []
    streams = data.get("streams") or []
    for st in streams:
        if st.get("codec_type") == "video":
            h = int(st.get("height", 480) or 480)
            qual = f"{480 if h <= 480 else 540 if h <= 540 else 720 if h <= 720 else 1080 if h <= 1080 else 2160 if h <= 2160 else 4320 if h <= 4320 else 8640}p"
            break
    for st in streams:
        lc = (st.get("tags") or {}).get("language")
        if not lc:
            continue
        name = _lang_name(lc)
        if st.get("codec_type") == "audio" and name not in langs:
            langs.append(name)
        elif st.get("codec_type") == "subtitle" and name not in subs:
            subs.append(name)
    return duration, qual, ", ".join(langs), ", ".join(subs)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _balance_html(text):
    """Close dangling tags / drop stray closers left after removing lines (Leechv2 logic)."""
    stack = []
    pat = re.compile(r"<(/)?(\w+)(?:\s[^>]*)?(\s*/)?>")
    out = text
    for m in pat.finditer(text):
        closing, name, selfclose = m.group(1) == "/", m.group(2).lower(), m.group(3) is not None
        if selfclose:
            continue
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            else:
                out = out.replace(m.group(0), "", 1)
        else:
            stack.append(name)
    while stack:
        out += f"</{stack.pop()}>"
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", out).strip()


async def build_caption(template, probe_path, filename, size, media=None):
    """
    Returns (caption_html, media_tuple). `media` can be passed back in to avoid re-probing
    (split parts share one probe). ffprobe / md5 only run when the template needs them.
    """
    if not template:
        return f"<b>{_html.escape(filename)}</b>", media
    t = re.sub(r"(\\\||\\\{|\\\}|\\s)",
               lambda m: {r"\|": "%%", r"\{": "&%&", r"\}": "$%$", r"\s": " "}[m.group(0)], template)
    parts = re.split(r"\|(?=[^|]*?:[^|]*?(?::\d+)?(?:\||$))", t)
    main = re.sub(r"\{([^}]+)\}", lambda m: "{" + m.group(1).lower() + "}", parts[0])

    video = is_video_file(probe_path)
    has_vph = any(ph in main for ph in VIDEO_PH)
    if has_vph and not video:
        main = "\n".join(l for l in main.split("\n") if not any(ph in l for ph in VIDEO_PH))

    if video and any(ph in main for ph in VIDEO_PH):
        if media is None:
            media = await get_media_info(probe_path)
        dur, qual, lang, subs = media
    else:
        dur, qual, lang, subs = 0, "", "", ""

    md5 = ""
    if "{md5_hash}" in main:
        try:
            md5 = await asyncio.to_thread(_md5, probe_path)
        except OSError:
            md5 = ""
    try:
        cap = main.format(filename=_html.escape(filename), size=human_size(size),
                          duration=human_time(dur), quality=qual, languages=lang,
                          subtitles=subs, md5_hash=md5)
    except (KeyError, IndexError, ValueError):
        return f"<b>{_html.escape(filename)}</b>", media

    if has_vph and not video:
        cap = _balance_html(cap)
    for part in parts[1:]:
        a = part.split(":")
        try:
            cap = cap.replace(a[0], a[1] if len(a) > 1 else "", int(a[2]) if len(a) == 3 else -1)
        except Exception:
            pass
    cap = re.sub(r"%%|&%&|\$%\$", lambda m: {"%%": "|", "&%&": "{", "$%$": "}"}[m.group()], cap)
    return cap[:1024], media      # Telegram caption limit


def validate_template(template):
    """'' if OK, else a message about unknown placeholders."""
    names = set(re.findall(r"\{([^{}]*)\}", template.replace("\\{", "").replace("\\}", "")))
    bad = sorted(n for n in names if n.split("|", 1)[0].strip().lower() not in VALID_FIELDS)
    return f"unknown placeholder(s): {', '.join('{'+b+'}' for b in bad)}. Valid: " + " ".join("{"+f+"}" for f in VALID_FIELDS) if bad else ""
