# 🚀 LDM — Aria2 · qBittorrent · yt-dlp Leech Bot

Telegram leech bot (wzgram / Pyrogram) that downloads with **Aria2**, **qBittorrent** or **yt-dlp**, then uploads to Telegram. Leechv2-style commands, dashboard, name rules, captions and per-user settings.

[![Developer](https://img.shields.io/badge/Developer-Goutham%20Josh-blue.svg?style=for-the-badge&logo=telegram)](https://t.me/im_goutham_josh)

## ✨ Features

- **Engines:** Aria2 (direct links, magnets), qBittorrent 5.x (magnets / `.torrent`, all files of multi-file torrents), yt-dlp (YouTube etc., quality picker, playlists, cookies)
- **Leechv2 dashboard:** `/status` with ⬢⬡ bars, speed, ETA, engine, CPU / RAM / disk footer, refresh and overview buttons
- **Uploads:** each task sends its files one by one; different tasks upload at the same time and share bandwidth. Auto retry (FloodWait / network), auto split over `SPLIT_SIZE` (`.001 .002 …`)
- **Per-user settings** (`/settings`): upload as Document or Video, thumbnail, leech dump chat, prefix / suffix / name swap, caption, yt-dlp cookies. Saved in MongoDB, or a local JSON file if no `DATABASE_URL`
- **Extract:** zip / 7z / tar / tar.gz / tar.bz2 / tar.xz archives with `/unzipleech` (`-e`) using py7zr, zipfile and tarfile. No `.rar`
- **Safety:** owner + authorized users / chats only, disk-space check, cleanup of leftovers, cancel at any stage
- **Auto "/" command menu** pushed to Telegram on every start

## ⌨️ Commands

| Command | What |
|---|---|
| `/leech` `/l` · `/unzipleech` `/uzl` | Aria2 direct link (+ extract) |
| `/qbleech` `/ql` `/qb` · `/qbunzipleech` `/quzl` | qBittorrent magnet / `.torrent` (+ extract) |
| `/aria` `/al` | Aria2 magnet / `.torrent` |
| `/ytdl` `/y` · `/ytdlleech` `/yl` | yt-dlp |
| `/status` `/s` · `/statusall` `/sall` | dashboard |
| `/cancel_<gid>` `/c_<gid>` · `/cancelall` `/call` | cancel |
| `/settings` `/usetting` `/us` | user settings panel |
| `/setthumb` `/delthumb` | thumbnail (reply to a photo) |
| `/setdump <chat_id\|here>` `/deldump` | leech dump chat |
| `/setswap` `/setprefix` `/setsuffix` `/setcaption` | filename rules and caption (`clear` removes) |
| `/setcookies` `/delcookies` | yt-dlp cookies |
| `/l` (reply to a Telegram file) | re-upload a Telegram file with rename / extract / zip / caption rules |
| `/l -i 5` (reply to the first file/link) | multi: leech that message and the next 4 (max 20, one task each) |
| `/ql -i 5` (reply to the first magnet / `.torrent`) | multi qBit: leech that message and the next 4 (max 20, one task each) |
| `/stats` `/st` · `/ping` · `/help` `/h` | info |
| `/authorize` `/a` · `/unauthorize` `/ua` | owner: allow / remove a user or chat |
| `/log` · `/restart` `/r` `/restartall` | owner: log file, reboot |

Flags: `-z [password]` pack into one archive (no password = `.zip` stored, with password = AES-256 `.7z`; works on `/l` `/qb` `/aria` `/y`) · `-i N` multi · `-e` extract · `-n <name>` rename (single file, keep last) · yt-dlp: `-q 720|best|audio|mp3|<format>` `-np` (no playlist)

## 🔤 Name rules

- **Swap:** `/setswap pattern:replacement|pattern2:replacement2[:count]`, regex on the filename, extension untouched, empty replacement deletes. Example: `/setswap \[.*?\]::|www\.\S+::|Hindi:Tamil`
- **Prefix / suffix:** `/setprefix @channel\s` · `/setsuffix \s[1080p]` (`\s` = space)
- Defaults for everyone: `LEECH_PREFIX`, `LEECH_SUFFIX`, `LEECH_NAME_SWAP`. Applies to every engine and extracted files; a manual `-n` on a single file skips them.

## 📝 Caption

Default (`LEECH_CAPTION`), per-user override `/setcaption <template>`:

```
<b><i>{filename}</i></b>

Size🎲 : <i>{size}</i>
Quality 💡: <i>{quality}</i>
Lang 🧩: <i>{languages}</i>
Sub 📄: <i>{subtitles}</i>
```

Placeholders: `{filename} {size} {duration} {quality} {languages} {subtitles} {md5_hash}`. Replace text inside a value: `{filename|old:new}`. Lines using video-only values (quality, languages, subtitles, duration) are dropped for non-video files. Quality / language / subtitles come from ffprobe.

## ▶️ yt-dlp and cookies

- `/y <url>` shows quality buttons (heights the video really has, MP3 192/320, M4A, best video / audio). If YouTube says "format is not available" the bot retries with looser formats, other player clients, then without cookies.
- **Cookies:** reply to a Netscape `cookies.txt` with `/setcookies` (saved per user in MongoDB). Owner: `/setcookies default` for a bot-wide file (restored on every start). `/settings` toggles "default / mine". `YTDL_COOKIES` = default file path. Extra options: `YTDL_OPTIONS='{"proxy": "http://..."}'`
- YouTube needs a JS runtime: **deno** (installed by the Dockerfile and `setup.sh`; not available on Termux, so use cookies there).

## 🧲 qBittorrent

Local `qbittorrent-nox` started by `start.sh` (WebUI on `127.0.0.1:8090`, localhost auth bypass), controlled through `aioqbt`. Low-RAM libtorrent profile (POSIX disk IO, small caches), duplicate cleanup, dead-magnet timeout (10 min). If `qbittorrent-nox` is missing, only `/qb` is disabled.

## 🛠️ Install & run

Fill the values first: edit `config.py` or set environment variables (table below).

### 🐳 Docker (Koyeb, Render, VPS, local)

```bash
git clone https://github.com/GouthamSER/ldm && cd ldm
docker build -t ldm-bot .
docker run -d --name ldm \
  -e API_ID=123456 -e API_HASH=xxxx -e BOT_TOKEN=123:abc -e OWNER_ID=111111 \
  -e DATABASE_URL="mongodb+srv://..." \
  -p 8000:8000 ldm-bot
```

On Koyeb / Render choose the **Dockerfile** builder and set the same variables in the dashboard. Port `8000` is the health check.

### 🖥️ VPS without Docker (Debian / Ubuntu)

```bash
bash setup.sh    # once: apt deps, qbittorrent-nox, deno, .venv, pip install
bash start.sh    # starts aria2 + qBittorrent, then the bot
```

`python3 bot.py` alone works only if aria2 (RPC `6801`) and qbittorrent-nox are already running.

### 📱 Termux (Android, no root)

```bash
pkg install git -y && git clone https://github.com/GouthamSER/ldm && cd ldm
bash termux.sh   # once
bash start.sh
```

- Run `termux-wake-lock` so Android keeps the bot alive; `tmux` keeps it running after you close the session.
- No deno: some YouTube links need cookies (`/setcookies`). If `qbittorrent-nox` is not in the repo, `/qb` is disabled and aria2 still works.

### ☁️ Buildpack (Heroku-style)

Files: `Procfile`, `runtime.txt`, `Aptfile` (aria2, ffmpeg, psmisc; needs the apt buildpack), `bin/post_compile` (downloads static qBittorrent and deno into `./bin`). `start.sh` puts `./bin` and `.apt` on `PATH`. If your platform ignores `Aptfile`, use the Docker route instead.

## 🔐 Secrets: `config.env`

Put `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATABASE_URL` … in **`config.env`** (copy `config.sample.env`), not in `config.py`. `config.env` is in `.gitignore` and is never touched by auto-update, so a restart can never wipe your credentials. Real environment variables (Docker `-e`, Koyeb dashboard) override it.

Already edited `config.py` directly? On the first auto-update, the values you changed (API_ID, API_HASH, BOT_TOKEN, OWNER_ID …) are copied into `config.env` before `config.py` is replaced. If that cannot be verified, your old `config.py` is kept instead. A copy always goes to `.update_backup/`.

## 🔧 Environment variables

| Variable | Default | Description |
|---|---|---|
| `API_ID` `API_HASH` `BOT_TOKEN` | — | Telegram credentials (required) |
| `OWNER_ID` | placeholder | Your user ID. Bot is open to everyone while this is the placeholder |
| `AUTH_USERS` `AUTH_CHATS` | — | Extra allowed user / chat IDs (comma separated) |
| `ARIA2_HOST` `ARIA2_PORT` `ARIA2_SECRET` | `127.0.0.1` `6801` `gjxdml` | Aria2 RPC (keep port `6801`) |
| `QBIT_HOST` `QBIT_PORT` `QBIT_USERNAME` `QBIT_PASSWORD` | `127.0.0.1` `8090` | qBittorrent WebUI (empty user = localhost bypass) |
| `PORT` | `8000` | Health-check web server |
| `DOWNLOAD_DIR` | `./downloads` | Download folder |
| `SESSION_NAME` | `ariadwd` | Pyrogram session name |
| `DATABASE_URL` `DATABASE_NAME` | — `ldmbot` | MongoDB for user settings, cookies, auth list |
| `MAX_UPLOADS` | `0` | Max files uploading at once across all tasks (`0` = no cap) |
| `MAX_TRANSMISSIONS` | `1` | Parallel part streams per upload (1 = safest vs FLOOD_WAIT) |
| `SPLIT_SIZE` | 1.95 GiB | Split bigger files (Premium: 3.9 GiB, `0` = never) |
| `LEECH_CAPTION` | see above | Default caption template |
| `LEECH_DUMP` | `0` | Upload into this chat instead of the command chat |
| `LEECH_PREFIX` `LEECH_SUFFIX` `LEECH_NAME_SWAP` | — | Default name rules |
| `YTDL_COOKIES` `YTDL_OPTIONS` | `cookies.txt` | yt-dlp default cookies file / extra options (dict literal) |
| `UPSTREAM_REPO` `UPSTREAM_BRANCH` `UPDATE_KEEP_FILES` | — `main` — | Auto update, see below |
| `SET_COMMANDS` | `True` | Push the "/" menu on start |
| `ENGINE_NAME` | auto | Override the engine label in messages |

## 🔄 Auto update (like Leechv2)

On every start and every `/restart`, `update.py` pulls the newest code from your repo, then starts the bot. Set:

| Variable | Description |
|---|---|
| `UPSTREAM_REPO` | Git URL. Private repo: `https://<token>@github.com/user/repo`. Empty = no update |
| `UPSTREAM_BRANCH` | Branch, default `main` |
| `UPDATE_KEEP_FILES` | Local files the update must not overwrite, e.g. `config.py,cookies.txt` (default none; `config.py` is always backed up to `.update_backup/`) |
| `UPDATE_PIP` | `true` always / `false` never reinstall requirements (default: only when `requirements.txt` changed) |

It runs `git fetch` + `git reset --hard`, so local edits to tracked files are replaced. Tokens are hidden in logs. If the update fails the bot still starts with the current code. Needs `git` (already in the Docker image, `setup.sh` and `termux.sh`).

## 🩺 Troubleshooting

| Message | Meaning |
|---|---|
| `Requested format is not available` | YouTube hides formats. Update yt-dlp, make sure `deno` exists, set fresh cookies |
| `FLOOD_WAIT … SaveBigFilePart` | Telegram rate limit, retried automatically. Lower `MAX_UPLOADS`, keep `MAX_TRANSMISSIONS=1` |
| `Upload progress callback error: … cancelled by user` | Normal after `/cancel`, harmless |
| `Connection closed by the server` | Telegram dropped the socket, the client reconnects |
| `Client clock is Ns out of step` | Host clock drift, auto-corrected |
| `Session start attempt N failed` | Telegram unreachable at startup. Check network, one instance only, delete a stale `.session` |
| qBit "never showed up" | Check `qbit.log`; confirm `qbittorrent-nox` runs on port `8090` |

## 🤝 Credits

Maintained by [Goutham Josh](https://t.me/im_goutham_josh). Dashboard, name rules, caption and command set follow Leechv2. ⭐ Star the repo if it helps.
