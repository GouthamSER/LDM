#!/usr/bin/env python3
"""
Self-update on every start / restart (like Leechv2's update.py).

  UPSTREAM_REPO    git URL to pull code from (private repo: https://<token>@github.com/user/repo)
                   empty = no update, bot starts as is
  UPSTREAM_BRANCH  branch to pull (default: main)
  UPDATE_KEEP_FILES  comma list of local files to keep untouched, e.g. "cookies.txt"
                     (default: none; config.py is always backed up to .update_backup/)

Secrets live in config.env (never touched by the update; see config.sample.env). If you edited
API_ID / API_HASH / BOT_TOKEN directly inside config.py, the first update copies those edited
values into config.env before replacing config.py. If that cannot be verified, your old
config.py is put back instead, so the bot never loses its credentials.
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


_DUMP = (
    "import json, config\n"
    "out = {}\n"
    "for k in dir(config):\n"
    "    if not k.isupper():\n"
    "        continue\n"
    "    v = getattr(config, k)\n"
    "    if isinstance(v, (str, int, float, bool)):\n"
    "        out[k] = v\n"
    "    elif isinstance(v, list) and all(isinstance(x, int) for x in v):\n"
    "        out[k] = ','.join(map(str, v))\n"
    "print('@@' + json.dumps(out))\n"
)


def _dump_config():
    """Values config.py yields with a CLEAN environment and no config.env -> dict, or None."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "PYTHONPATH") if k in os.environ}
    env["CONFIG_ENV_FILE"] = os.devnull
    try:
        r = subprocess.run([sys.executable, "-c", _DUMP], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        for line in reversed(r.stdout.splitlines()):
            if line.startswith("@@"):
                import json
                return json.loads(line[2:])
    except Exception:
        pass
    return None


def _env_names():
    """Names config.py reads from the environment (so they can live in config.env)."""
    try:
        with open(os.path.join(ROOT, "config.py"), encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return set()
    return set(re.findall(r'get_env_variable\(\s*"(\w+)"', txt)) | set(re.findall(r'_id_list\("(\w+)"', txt))


def _migrate(local, new):
    """Values you edited inside the OLD config.py -> config.env, so the update can replace
    config.py without losing API_ID / API_HASH / BOT_TOKEN. Returns True when safe."""
    if local is None or new is None:
        return False
    names = _env_names()
    diff = {k: v for k, v in local.items()
            if k in names and k in new and str(v) != str(new[k]) and "\n" not in str(v)}
    path = os.path.join(ROOT, "config.env")
    have = set()
    try:
        with open(path, encoding="utf-8") as f:
            have = {ln.split("=", 1)[0].strip() for ln in f if "=" in ln and not ln.lstrip().startswith("#")}
    except OSError:
        pass
    add = {k: v for k, v in diff.items() if k not in have}
    if add:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n# migrated from your old config.py by update.py\n")
            for k, v in add.items():
                f.write(f"{k}={v}\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print(f"🔁 Moved {len(add)} value(s) you had edited in config.py into config.env: "
              + ", ".join(sorted(add)) + " (values hidden)")
    return True


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

    local_cfg = _dump_config()                      # what the current config.py yields
    cfg_before = _sha("config.py")
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
        # config.py was just replaced: carry your edits over into config.env, or put the old file back
        if "config.py" not in keep and _sha("config.py") != cfg_before:
            ok = False
            try:
                ok = _migrate(local_cfg, _dump_config())
            except Exception as e:
                print("⚠️ config migration error:", e)
            if not ok:
                bak = os.path.join(bdir, "config.py")
                if os.path.isfile(bak):
                    shutil.copy2(bak, os.path.join(ROOT, "config.py"))
                    print("⚠️ Could not verify your config values: kept your OLD config.py. "
                          "Move your secrets into config.env (see config.sample.env).")
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
