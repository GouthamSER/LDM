FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    aria2 ffmpeg curl ca-certificates lsof net-tools psmisc procps \
    build-essential python3-dev libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# qBittorrent 5.x — static qbittorrent-nox build (no libs, ~70MB)
RUN set -eux; \
    arch="$(uname -m)"; \
    curl -fsSL -o /usr/local/bin/qbittorrent-nox \
      "https://github.com/userdocs/qbittorrent-nox-static/releases/latest/download/${arch}-qbittorrent-nox"; \
    chmod +x /usr/local/bin/qbittorrent-nox; \
    qbittorrent-nox --version

RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh && mkdir -p downloads && chown -R appuser:appuser /app
USER appuser

EXPOSE 6801 8000
ENTRYPOINT ["./start.sh"]
