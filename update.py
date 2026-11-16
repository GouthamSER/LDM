#!/usr/bin/env python3
"""
Self-update on every start / restart (like Leechv2's update.py).

  UPSTREAM_REPO    git URL to pull code from (private repo: https://<token>@github.com/user/repo)
                   empty = no update, bot starts as is
  UPSTREAM_BRANCH  branch to pull (default: main)
  UPDATE_KEEP_FILES  comma list of local files to keep untouched, e.g. "config.py,cookies.txt"
                     (default: none; config.py is always backed up to .update_backup/)
  UPDATE_PIP       "true" = always pip install -r requirements.txt, "false" = never,
                   unset = only when requirements.txt changed

Usage:  python3 update.py          -> update only
        python3 update.py --run    -> update, then replace itself with `python3 bot.py`
Never fails the boot: any error is printed and the current code keeps running.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)


def _cfg(name, default=""):
    v = os.environ.get(name)
    if v is not None and v != "":
        return v.strip()
    try:                                   # fall back to config.py values
        sys.path.insert(0, ROOT)
        import config
        return str(getattr(config, name, default) or default).strip()
    except Exception:
        return default


def _mask(text, secret_src):
    """Hide any token in https://token@host URLs from log output."""
    return re.sub(r"(https?://)[^@/\s]+@", r"\1***@", text or "")


def _git(*args, timeout=180):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def update():
    repo = _cfg("UPSTREAM_REPO")
    branch = _cfg("UPSTREAM_BRANCH", "main") or "main"
    if not repo:
        print("ℹ️ UPSTREAM_REPO not set: skipping update")
        return
    if not shutil.which("git"):
        print("⚠️ git not installed: skipping update")
        return

    req_before = _sha("requirements.txt")
    keep = [x.strip() for x in _cfg("UPDATE_KEEP_FILES").split(",") if x.strip()]

    # backups: config.py always, KEEP files restored afterwards
    bdir = os.path.join(ROOT, ".update_backup")
    os.makedirs(bdir, exist_ok=True)
    saved = {}
    for name in set(keep) | {"config.py"}:
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            dst = os.path.join(bdir, name.replace("/", "__"))
            shutil.copy2(src, dst)
            if name in keep:
                saved[name] = dst

    try:
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            _git("init")
        r = _git("remote", "set-url", "origin", repo)
        if r.returncode != 0:
            _git("remote", "add", "origin", repo)
        print(f"⬇️ Updating from {_mask(repo, repo)} ({branch}) ...")
        f = _git("fetch", "--depth", "1", "origin", branch)
        if f.returncode != 0:
            print("❌ git fetch failed:", _mask(f.stderr.strip()[-300:], repo))
            return
        r = _git("reset", "--hard", "FETCH_HEAD")
        if r.returncode != 0:
            print("❌ git reset failed:", _mask(r.stderr.strip()[-300:], repo))
            return
        log = _git("log", "-1", "--pretty=%h %s")
        print("✅ Updated to:", _mask(log.stdout.strip(), repo))
    except Exception as e:
        print("❌ update error:", _mask(str(e), repo))
        return
    finally:
        for name, bak in saved.items():          # keep-files win over the pulled copy
            try:
                shutil.copy2(bak, os.path.join(ROOT, name))
            except OSError:
                pass

    # dependencies
    pip_mode = _cfg("UPDATE_PIP").lower()
    changed = _sha("requirements.txt") != req_before
    if pip_mode == "true" or (pip_mode != "false" and changed):
        print("📦 requirements changed: pip install -r requirements.txt")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
                        "--break-system-packages", "-q"], cwd=ROOT)


def main():
    try:
        update()
    except Exception as e:                       # never block the boot
        print("❌ update.py crashed:", e)
    if "--run" in sys.argv:
        os.execv(sys.executable, [sys.executable, os.path.join(ROOT, "bot.py")])


if __name__ == "__main__":
    main()
