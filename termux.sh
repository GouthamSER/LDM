#!/data/data/com.termux/files/usr/bin/bash
# Termux install (no root, no Docker):   bash termux.sh   then   bash start.sh
set -e
pkg update -y && pkg upgrade -y
pkg install -y python aria2 ffmpeg curl unzip psmisc procps git \
    clang make rust binutils libffi openssl libjpeg-turbo libpng freetype
# qBittorrent (optional: if the package is missing, only /qb is disabled)
pkg install -y qbittorrent-nox || echo "⚠️ qbittorrent-nox not in repo: /qb disabled, aria2 still works"

pip install --upgrade pip
grep -viE '^psutil' requirements.txt > /tmp/req-termux.txt
LDFLAGS="-L/data/data/com.termux/files/usr/lib" pip install -r /tmp/req-termux.txt
pip install psutil || echo "⚠️ psutil failed (dashboard CPU/RAM show 0, harmless)"

echo "✅ done. NOTE: deno has no Termux build -> some YouTube links may need cookies (/setcookies)."
echo "▶️  run: bash start.sh"
