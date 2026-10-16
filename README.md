# TEI Player — Full Project

Includes the Vercel Next.js frontend, Flask/Gunicorn API, Pyrogram Telegram bot, Dockerfiles, Docker Compose, MongoDB, FFmpeg/HLS packaging, expiry cleanup, and deployment guides.

## Important deployment answer

**Vercel cannot host this whole architecture by itself.**
- Vercel hosts the Next.js frontend and share pages.
- The VPS runs the Flask API, Telegram bot, FFmpeg and media files.
- MongoDB Atlas is used as the external database; Docker Compose does not start a second MongoDB server.
- Docker Compose starts the backend services on the VPS. Vercel deployment is separate and connects to the public HTTPS API.

The frontend is designed with the agreed TEI Player visual direction: dark navy/black background, neon-blue accents, cinematic player, controls inside the video, simple original-file metadata, expiry countdown, and separate buttons for Download Original File, Copy HLS URL and External Players. The desktop layout is retained and the page is responsive on mobile.

## Architecture

Telegram upload → bot downloads original to shared VPS storage → bot asks Flask to register it → Flask probes with FFprobe and tries HLS packaging using FFmpeg stream copy → bot replies with Vercel player link → browser streams HLS directly from the VPS API.

No video re-encoding is performed. HLS is marked unavailable if packaging fails. HLS does not create extra quality levels; only the original source's streams are detected. The original file remains downloadable until expiry.

## Requirements

- Ubuntu 24.04 recommended
- Docker Engine + Docker Compose plugin on VPS
- GitHub account
- Vercel account
- Telegram bot token, Telegram API ID and API hash
- A public HTTPS URL for the API (free options depend on provider availability; a domain is not included)
- A MongoDB Atlas cluster and connection URL

## 1. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill `MONGODB_URI` with your MongoDB Atlas connection string (including `/tei_player` before query parameters), and set a strong unique `BOT_API_TOKEN`. Fill `API_ID`, `API_HASH`, and `BOT_TOKEN`. Do not use the Atlas password previously pasted into chat; rotate it first and use the new URL-encoded password. Set `PLAYER_BASE_URL` after Vercel gives you a URL. Set `CORS_ORIGINS` to that exact Vercel origin. `PUBLIC_API_BASE_URL` must be the public HTTPS API origin, with no trailing slash.

Never commit `.env`, bot session files, database data or private credentials.

## 2. Deploy API + bot on the VPS

Install Docker and the Compose plugin if not already installed. From this project directory:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f api bot
```

The API is published only on `127.0.0.1:5055` by default. Put a TLS reverse proxy or a suitable HTTPS tunnel in front of it. Do not expose Flask's development server. Once HTTPS is configured, set `PUBLIC_API_BASE_URL=https://your-api-host` and restart:

```bash
docker compose up -d
```

For a temporary local check on the VPS:
```bash
curl http://127.0.0.1:5055/health
```

For production, do not leave the API reachable over plain public HTTP, especially because media links are bearer links.

### Free HTTPS notes

- Vercel provides a free `*.vercel.app` frontend hostname.
- The API needs its own publicly reachable HTTPS endpoint. A free tunnel can be used for testing; its hostname may change or have usage limitations. A stable custom hostname may require a domain or a provider-specific free hostname. Do not assume a stable, permanent, free endpoint is guaranteed.
- If using Cloudflare Tunnel, configure the tunnel to forward to `http://127.0.0.1:5055`; custom-hostname requirements depend on the Cloudflare setup.

## 3. Deploy frontend to Vercel from GitHub

1. Push this project to GitHub.
2. In Vercel, import the repository.
3. Set **Root Directory** to `frontend`.
4. Add environment variable `NEXT_PUBLIC_API_BASE_URL` with the public HTTPS API URL, no trailing slash.
5. Deploy.

After the first deploy, copy the Vercel origin (e.g. `https://your-project.vercel.app`) into VPS `.env`:
- `PLAYER_BASE_URL`
- `CORS_ORIGINS` (origin only, no path)

Then restart the VPS stack:
```bash
docker compose up -d
```

Vercel can automatically redeploy the frontend on GitHub pushes. This does not deploy or update the VPS containers; run `docker compose up -d --build` on the VPS after backend changes.

## 4. Bot use

Open the Telegram bot and send a video or document. It downloads the original file to the shared `/media` volume, registers it, attempts HLS stream-copy packaging, and returns the player URL. The configured per-file cap is 4 GiB (`MAX_DOWNLOAD_BYTES` and `MAX_UPLOAD_BYTES`).

**Important large-file limitation:** this setting is only the application cap, not a guarantee that Telegram will deliver or Pyrogram can download every 4 GB file. Telegram client/API limits and the exact sender/account/file path may impose a lower limit. Test with a non-sensitive large file before relying on 4 GB uploads. The VPS must have enough free disk space for the original plus HLS segments and concurrent uploads. Keep only media you are authorized to distribute.

## API endpoints

- `GET /health`
- `POST /api/media/register` (requires `X-Bot-Token`)
- `GET /api/media/<share_id>`
- `GET /api/media/<share_id>/master.m3u8`
- `GET /api/media/<share_id>/hls/<filename>`
- `GET /api/media/<share_id>/download`

## Retention and storage

Default retention is 6 hours. `cleanup` service runs every 10 minutes and deletes both database metadata and physical files under the managed media directory. Adjust `FILE_RETENTION_HOURS` and `MAX_UPLOAD_BYTES` in `.env`. Monitor disk usage before sharing widely.

## Security checklist

- Use HTTPS for public API and HLS.
- Keep secrets out of GitHub.
- Rotate any MongoDB credentials that were shared or committed.
- Set CORS to your exact Vercel origin.
- Keep the MongoDB Atlas connection string out of GitHub and never commit `.env`.
- Keep API port bound to loopback behind a TLS proxy/tunnel.
- Share IDs are bearer links: anyone possessing a valid link can access the media until expiry.
- The bot API token is required for registration.
- This starter is not a substitute for a security review. Test concurrency, large files, expiry, and browser playback before a public launch.
