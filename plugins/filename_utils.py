# plugins/filename_utils.py
"""
Filename rules ported 1:1 from Leechv2 (bot/helper/ext_utils/filename_utils.py):
  www.site.tld removal  ->  name swap (regex)  ->  prefix  ->  suffix
"""

import logging
from os import path as ospath
from re import sub as re_sub

import config

LOGGER = logging.getLogger(__name__)


def effective_rules(prefs):
    """User value wins; empty falls back to config default (same as Leechv2)."""
    return (
        (prefs.get("prefix") or config.LEECH_PREFIX or ""),
        (prefs.get("suffix") or config.LEECH_SUFFIX or ""),
        (prefs.get("name_swap") or config.LEECH_NAME_SWAP or ""),
    )


def has_rules(prefs):
    return any(effective_rules(prefs))


def format_filename(file_name, prefs):
    prefix, suffix, name_swap = effective_rules(prefs)
    original_file = file_name

    # name_swap: regex |pattern:replacement[:count]
    if name_swap:
        try:
            if not name_swap.startswith("|"):
                name_swap = f"|{name_swap}"

            name_swap = name_swap.replace("\\s", " ")
            patterns = name_swap.split("|")

            name_without_ext, file_ext = ospath.splitext(file_name)

            for pattern_str in patterns[1:]:
                args = pattern_str.split(":")
                if len(args) >= 2:
                    pattern, replacement = args[0], args[1]
                    # Leechv2's own example (`pat::`) leaves count empty -> int('') crashed; treat as 0
                    count = int(args[2]) if len(args) > 2 and args[2].strip().isdigit() else 0
                    try:
                        name_without_ext = re_sub(pattern, replacement, name_without_ext, count)
                    except Exception as e:
                        LOGGER.error(f"Name Swap pattern error: {e}")
                elif len(args) == 1 and args[0]:
                    try:
                        name_without_ext = re_sub(args[0], "", name_without_ext)
                    except Exception as e:
                        LOGGER.error(f"Name Swap removal error: {e}")

            file_name = name_without_ext + file_ext
        except Exception as e:
            LOGGER.error(f"Name Swap processing error: {e}")
            file_name = original_file

    # built-in cleanup runs AFTER the user's swap, so a rule like [www.site.xyz] can still
    # match its brackets before the bare domain is stripped
    file_name = re_sub(
        r"www\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,12}(?:\.[a-zA-Z]{2,3})?",
        "",
        file_name,
    )
    file_name = re_sub(r"^\s*[-_~]+\s*", "", file_name).strip()

    if prefix:
        prefix_clean = prefix.replace("\\s", " ")
        prefix_check = re_sub(r"<.*?>", "", prefix_clean)
        if not file_name.startswith(prefix_check):
            file_name = f"{prefix_clean}{file_name}"

    if suffix:
        suffix_clean = suffix.replace("\\s", " ")
        name_without_ext, file_ext = ospath.splitext(file_name)
        file_name = f"{name_without_ext}{suffix_clean}{file_ext}"

    # a filename can't hold HTML tags or "/" (Leechv2 keeps tags for the caption only)
    file_name = re_sub(r"<.*?>", "", file_name).replace("/", "_").strip()
    return file_name or original_file


def validate_swap(pattern_str):
    """Return an error string if any regex in a name-swap string is invalid, else ''."""
    import re
    s = pattern_str.replace("\\s", " ")
    for part in s.split("|"):
        if not part:
            continue
        pat = part.split(":")[0]
        try:
            re.compile(pat)
        except re.error as e:
            return f"bad regex `{pat}`: {e}"
    return ""
