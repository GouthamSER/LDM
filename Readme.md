# 🚀 Aria2 Leech Telegram Bot

**A high-performance Telegram bot built with Pyrogram and integrated with Aria2 for fast, direct-link downloading (leeching) and seamless uploading to Telegram chats.**

[![Repo Owner](https://img.shields.io/badge/Developer-Goutham%20Josh-blue.svg?style=for-the-badge&logo=telegram)](https://t.me/im_goutham_josh)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

<br>
<p align="center">
  <img src="https://img.shields.io/badge/Fork%20this%20light-weight%20repo%20⭐-Click%20here-blue?style=for-the-badge&logo=github" alt="Fork Repo">
</p>
<br>

## ✨ Features

- **⚡ Fast Downloading**: Utilizes **Aria2** for high-speed, multi-connection downloads
- **📊 Real-time Progress**: Live updates with speed, ETA, and interactive progress bars (Download → Upload)
- **📋 Consolidated Status** (`/status`): View all active downloads/uploads in a single refreshable message
- **🔄 Concurrent Tasks**: Multiple simultaneous downloads/uploads using threading & asyncio
- **💻 System Stats** (`/stats`): CPU, RAM, Disk usage, and bot activity metrics
- **❌ Cancellation**: Gracefully cancel active download/upload tasks
- **🩺 Health Check**: Web endpoint for cloud deployment monitoring

## 🔀 Upload Engine

Two selectable engines, both drop-in `pyrogram` forks — zero code changes to switch:

| Engine | requirements.txt line | Notes |
|---|---|---|
| **pyroblack** (default) | `pyroblack` | stable, feature-rich fork |
| **wzgram[fast]** | `wzgram[fast]` | rust WarpCrypto backend, faster |

Open `requirements.txt`, comment/uncomment the engine you want, redeploy. The bot auto-detects which one is installed and shows it in progress messages — no env var needed (optional `ENGINE_NAME` env var can force a custom label).

## ⌨️ Commands (names match Leechv2) + auto "/" menu

The command list is pushed to Telegram on every start (`SET_COMMANDS=False` to disable).

| Command | What |
|---|---|
| `/leech` `/l` · `/unzipleech` `/uzl` | Aria2 direct link (+ extract) |
| `/qbleech` `/ql` `/qb` · `/qbunzipleech` `/quzl` | qBittorrent magnet / .torrent (+ extract) |
| `/aria` `/al` | Aria2 magnet / .torrent |
| `/ytdl` `/y` `/ytdlleech` `/yl` | yt-dlp |
| `/status` `/s` `/statusall` `/sall` | dashboard |
| `/cancel_<gid>` `/c_<gid>` · `/cancelall` `/call` | cancel |
| `/usetting` `/us` `/settings` | upload type, thumbnail, dump, name rules |
| `/stats` `/st` · `/ping` · `/help` `/h` | info |
| `/authorize` `/a` · `/unauthorize` `/ua` | owner: allow / remove a replied user, or this chat (saved) |
| `/log` · `/restart` `/r` `/restartall` | owner: bot log, reboot (with confirm buttons) |

## ▶️ yt-dlp (`/y`) + 🔤 Name swap (ported from Leechv2)

- `/y <url>` shows quality buttons (only heights the video really has, MP3 192/320, M4A, best video/audio). Skip them with `-q 720|best|audio|mp3|<yt-dlp format>`. `-np` ignores the playlist part of a URL, `-n <name>` renames (keep `-n` last). Playlists upload every video.
- **Cookies (like Leechv2):** reply to a Netscape `cookies.txt` with `/setcookies` → saved per user in MongoDB (`/delcookies` removes). Owner: `/setcookies default` sets a bot-wide file (restored from MongoDB on every start, so it survives Koyeb restarts). A user with personal cookies uses them; `/settings` has a toggle "Use default / Use mine" and shows which is active. Cookie files are deleted from the chat after saving. `YTDL_COOKIES` = where the default file lives. Extra options: `YTDL_OPTIONS='{"proxy": "http://..."}'`.
- **Name swap** `/setswap pattern:replacement|pattern2:replacement2[:count]` — regex on the filename, extension untouched; empty replacement deletes the match. Example: `/setswap \[.*?\]::|www\.\S+::|Hindi:Tamil`.
- `/setprefix @mychannel\s` and `/setsuffix \s[1080p]` (`\s` = space). Each takes `clear` to remove. Defaults for everyone via `LEECH_PREFIX` / `LEECH_SUFFIX` / `LEECH_NAME_SWAP`. Rules apply to every leech (aria2, qBit, yt-dlp, extracted files); a manual `-n` name on a single file skips them.
- Dashboard now uses the Leechv2 layout (⬢⬡ bar, Process / Processed / Status / Speed / Time / Engine / In Mode / Out Mode, ☰ overview and ♻️ refresh buttons, CPU / RAM / FREE · IN / OUT · DL / UL / UP footer). The completion report matches too.

## 🧲 qBittorrent engine (new)

`/qb <magnet | .torrent URL>` or reply to a `.torrent` file with `/qb`. Uses a local **qBittorrent 5.x** (`qbittorrent-nox`, started by `start.sh`) through its WebUI API (`aioqbt`). Aria2 stays for direct links (`/l`) and `/ql`.

- Low-RAM libtorrent profile (POSIX disk IO, small caches) — safe on 512MB hosts
- Multi-file torrents: **every** file is uploaded (old code uploaded only the first)
- Duplicate / leftover torrent auto-cleanup, dead-magnet timeout (10 min), disk-space check before download
- Works with `-e` (extract) and `-n <name>` (rename, single-file torrents)

## 🆕 Other additions

| Feature | Notes |
|---|---|
| Access control | `OWNER_ID`, `AUTH_USERS`, `AUTH_CHATS` (open only while `OWNER_ID` is the placeholder) |
| Upload retry | FloodWait + network errors retried (10s / 20s backoff) |
| Auto split | Files over `SPLIT_SIZE` (default 1.95GiB) upload as `.001 .002 …` |
| Caption template | `LEECH_CAPTION` — `{filename}` `{size}`, HTML allowed |
| Dump chat | `LEECH_DUMP` — upload to another chat/channel |
| Upload type + thumbnail | `/settings` (Document / Video), `/st` (alias), `/setthumb`, `/delthumb`, `/setdump <chat_id|here>`, `/deldump` (per-user leech dump chat). Saved per user in MongoDB (`DATABASE_URL`) or a local JSON fallback. Video mode sends streamable video with duration/size and an auto thumbnail |
| Reply-to-link | `/l` `/ql` `/qb` work when replying to a message containing a link |
| Completion report | Task size, time taken, mode, files uploaded, failures |
| `/cancelall`, `/ping`, `/help` | owner cancel-all, latency |
| Dashboard | stops refreshing when idle; shows seeders/peers; real engine versions |

## 🔧 Extra environment variables

`QBIT_HOST` `QBIT_PORT` (8090) `QBIT_USERNAME` `QBIT_PASSWORD` · `AUTH_USERS` `AUTH_CHATS` · `MAX_UPLOADS` (2) `MAX_TRANSMISSIONS` (1) · `SPLIT_SIZE` · `LEECH_CAPTION` · `LEECH_DUMP` · `SET_COMMANDS` · `DATABASE_URL` `DATABASE_NAME`

## ⚙️ Prerequisites

- Telegram API credentials (`API_ID` & `API_HASH`)
- Telegram Bot Token (`BOT_TOKEN`)
- Running **Aria2 RPC** daemon and **qbittorrent-nox** (both started by `start.sh`)
- Python 3.10+ environment

## 🛠️ Deployment Options

### 🚀 1. Docker (Recommended - Local & Cloud)

**A. Build the Docker Image**
git clone https://github.com/GouthamJosh/aria-tg
cd aria-tg
docker build -t aria2-leech-bot .

text

**B. Run the Container**
<br>
docker run -d<br>
--name aria2-leech<br>
-e API_ID="YOUR_API_ID"<br>
-e API_HASH="YOUR_API_HASH"<br>
-e BOT_TOKEN="YOUR_BOT_TOKEN"<br>
-e OWNER_ID="YOUR_OWNER_ID"<br>
-e ARIA2_HOST="127.0.0.1"<br>
-e ARIA2_PORT="6801"<br>
-e ARIA2_SECRET="gjxdml"<br>
-e PORT="8000"<br>
-p 8000:8000<br><br>


### 📱 2. Termux (Android)

**A. Install Dependencies**<br><br>
pkg update && pkg upgrade<br>
pkg install -y git python aria2
<br>

**B. Clone & Setup**<br>
aria2-leech-bot<br>

**C. Start Aria2 RPC** (in separate Termux session)
aria2c --enable-rpc --rpc-listen-all --rpc-secret=gjxdml --rpc-port=6801 -D<br><br>

**D. Run Bot**
bash start.sh<br><br>


### ☁️ 3. Cloud Platforms (Heroku/Render/Railway)

1. Fork the repository
2. Set environment variables in platform dashboard
3. Deploy with Docker support

## 🔧 Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `API_ID` | Telegram API ID | `12345678` |
| `API_HASH` | Telegram API Hash | `your_api_hash` |
| `BOT_TOKEN` | BotFather token | `123456:ABC-DEF...` |
| `OWNER_ID` | Your Telegram user ID | `123456789` |
| `ARIA2_HOST` | Aria2 RPC host | `127.0.0.1` |
| `ARIA2_PORT` | Aria2 RPC port | `6801` |
| `ARIA2_SECRET` | Aria2 RPC secret | `gjxdml` |
| `PORT` | Web server port | `8000` |

## 📚 Commands

- `/start` - Bot information
- `/status` - Active downloads/uploads
- `/stats` - System statistics
- `/cancel` - Cancel current task

## 🤝 Credits

**Maintained by [Goutham Josh](https://t.me/im_goutham_josh)**  
⭐ **Star this repo if it helps you!**

![Star History](https://star-history.com/#GouthamJosh/aria-tg&Date)


## Caption & uploads (Leechv2 style)
- Default caption (env `LEECH_CAPTION`, per-user `/setcaption <template>`, `/setcaption clear`):
  `{filename} {size} {duration} {quality} {languages} {subtitles} {md5_hash}`. Replace text: `{filename|old:new}`.
  Lines with video-only placeholders (quality/lang/sub/duration) are dropped for non-video files.
- Each task uploads its files one by one; different tasks upload at the same time and share bandwidth.
  `MAX_UPLOADS` (default `0` = no cap) optionally caps simultaneous uploads across all tasks.
