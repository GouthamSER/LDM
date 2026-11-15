#!/bin/bash
cd "$(dirname "$0")"
# Works in Docker or directly on a VPS (run `bash setup.sh` once first).
export PATH="$HOME/.local/bin:$PATH"
[ -f .venv/bin/activate ] && . .venv/bin/activate
PY=$(command -v python3 || command -v python)

for bin in aria2c qbittorrent-nox; do
  if ! command -v "$bin" &> /dev/null; then
    echo "❌ ERROR: $bin not found. Run: bash setup.sh"; exit 1
  fi
done

echo "🔪 Killing old aria2 / qBittorrent and freeing ports..."
pkill -9 -f aria2c || true
pkill -9 -f qbittorrent-nox || true
command -v fuser >/dev/null && fuser -k 6800/tcp 6801/tcp 8090/tcp || true
sleep 2

rm -rf downloads aria2.log aria2.session
mkdir -p downloads

ARIA2_SECRET=${ARIA2_SECRET:-gjxdml}
ARIA2_HOST=${ARIA2_HOST:-localhost}
QBIT_PORT=${QBIT_PORT:-8090}

# ---------- qBittorrent (WebUI on localhost only, auth bypass for 127.0.0.1) ----------
QPROF="$PWD/.qbit"
rm -rf "$QPROF"; mkdir -p "$QPROF/qBittorrent/config"
cat > "$QPROF/qBittorrent/config/qBittorrent.conf" <<QCONF
[LegalNotice]
Accepted=true

[Preferences]
WebUI\\Address=127.0.0.1
WebUI\\Port=${QBIT_PORT}
WebUI\\LocalHostAuth=false
WebUI\\CSRFProtection=false
WebUI\\HostHeaderValidation=false
Downloads\\SavePath=$PWD/downloads
QCONF
echo "🚀 Starting qBittorrent on 127.0.0.1:${QBIT_PORT}..."
qbittorrent-nox --profile="$QPROF" --confirm-legal-notice > qbit.log 2>&1 &

# ---------- Aria2 ----------
echo "🚀 Starting Aria2 on port 6801..."
aria2c \
    --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all=true \
    --rpc-secret="${ARIA2_SECRET}" --rpc-listen-port=6801 \
    --disable-ipv6 --dir=downloads \
    --max-connection-per-server=16 --split=16 --min-split-size=1M \
    --file-allocation=trunc --disk-cache=64M \
    --enable-dht=true --dht-listen-port=6881-6999 --bt-enable-lpd=true \
    --listen-port=6881-6999 \
    --log-level=info --log=aria2.log &

# wait (max ~30s) for both APIs
for i in $(seq 1 30); do
  A=0; Q=0
  curl -s -f -X POST -d "{\"jsonrpc\":\"2.0\",\"id\":\"t\",\"method\":\"aria2.getVersion\",\"params\":[\"token:${ARIA2_SECRET}\"]}" "http://${ARIA2_HOST}:6801/jsonrpc" >/dev/null 2>&1 && A=1
  curl -s -f "http://127.0.0.1:${QBIT_PORT}/api/v2/app/version" >/dev/null 2>&1 && Q=1
  [ "$A" = 1 ] && [ "$Q" = 1 ] && break
  sleep 1
done

if [ "$A" != 1 ]; then
  echo "❌ Aria2 FAILED on ${ARIA2_HOST}:6801"; tail -20 aria2.log 2>/dev/null; exit 1
fi
[ "$Q" = 1 ] && echo "✅ qBittorrent READY!" || { echo "⚠️ qBittorrent not up — /qb disabled. Log:"; tail -10 qbit.log; }
echo "✅ Aria2 READY!"
exec "$PY" bot.py
