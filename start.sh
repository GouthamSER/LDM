#!/bin/bash
# One-time install for running WITHOUT Docker (Debian/Ubuntu VPS, Codespaces, Termux-proot...).
#   bash setup.sh        then        bash start.sh
set -e
SUDO=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null && SUDO="sudo"
BIN="${BIN_DIR:-$HOME/.local/bin}"; mkdir -p "$BIN"
export PATH="$BIN:$PATH"

echo "📦 apt packages..."
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends aria2 ffmpeg curl unzip ca-certificates psmisc procps \
      python3 python3-pip python3-venv build-essential python3-dev libffi-dev libssl-dev
else
  echo "⚠️ no apt-get: install aria2, ffmpeg, curl, unzip, python3-pip yourself"
fi

arch="$(uname -m)"
if ! command -v qbittorrent-nox >/dev/null; then
  echo "📦 qbittorrent-nox (static)..."
  curl -fsSL -o "$BIN/qbittorrent-nox" "https://github.com/userdocs/qbittorrent-nox-static/releases/latest/download/${arch}-qbittorrent-nox"
  chmod +x "$BIN/qbittorrent-nox"
fi
if ! command -v deno >/dev/null; then
  echo "📦 deno (yt-dlp YouTube JS runtime)..."
  case "$arch" in aarch64) da=aarch64 ;; *) da=x86_64 ;; esac
  curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-${da}-unknown-linux-gnu.zip"
  unzip -qo /tmp/deno.zip -d "$BIN"; rm -f /tmp/deno.zip; chmod +x "$BIN/deno"
fi

echo "🐍 python packages (venv: .venv)..."
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "✅ done. Add to your shell if missing:  export PATH=\"$BIN:\$PATH\""
echo "▶️  run: bash start.sh"
