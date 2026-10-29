# TSBot (QQ Music Edition)

TSBot is a TeamSpeak music bot. The backend uses QQ Music for song search, playlists, lyrics, and playback URLs, while voice-service connects to TeamSpeak and plays audio.

> The repository directory and existing image names may still contain the historical NeteaseTSBot name for deployment compatibility. QQ Music is the only music source in the current version.

## Features

- TeamSpeak TS3/TS6 audio playback, volume, shuffle, repeat, skip, and seek controls
- QQ Music search, playlists, lyrics, albums, singers, and MV information
- QR-code or Cookie authorization for QQ Music, stored encrypted on the server
- Playback queue, history, local favorites, Web console, and TeamSpeak chat commands
- Token-protected external API for search, queue, status, and history operations

## Architecture

~~~text
[web: Vue 3] --HTTP--> [backend: FastAPI + QQ Music] --gRPC--> [voice-service: Rust] --> TeamSpeak
~~~

Main directories:

- backend/: FastAPI backend, QQ Music adapter, database, and voice client
- web/: Vue 3 + TypeScript administration console
- voice-service/: Rust TeamSpeak audio service
- tests/: backend regression tests
- proto/: gRPC definitions

## Quick Start

Linux, Python 3.10+, Node.js 16+, Rust 1.70+, build tools, and ffmpeg are required.

~~~bash
cp tsbot.env.example tsbot.env
# Set a strong TSBOT_COOKIE_KEY in tsbot.env
make all
~~~

On first startup, the backend creates a one-time admin password in logs/initial-admin-password.txt. Sign in to the Web console, change the password, and authorize QQ Music under System Settings → Music Authorization.

For local development, run the services separately:

~~~bash
./run-voicemake.sh
backend/.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8009
npm --prefix web run dev
~~~

The development Web UI defaults to http://127.0.0.1:5173 and the backend OpenAPI document is available at http://127.0.0.1:8009/docs.

## Docker

~~~bash
cp tsbot.env.example tsbot.env
# Set TSBOT_COOKIE_KEY
docker compose up -d --build
docker compose ps
~~~

The default ports are web 8080, backend 8009, and voice-service gRPC 50051. To use prebuilt images published by GitHub Actions:

~~~bash
docker compose -f docker-compose.prebuilt.yml up -d
# Pin to an image version from a GitHub Release:
TSBOT_IMAGE_TAG=vX.Y.Z docker compose -f docker-compose.prebuilt.yml up -d
~~~

When a push to `main` or `master` matches the publishing paths, Docker Publish builds all three service images. After they succeed, it increments the patch version of the latest stable `vX.Y.Z` tag, creates a GitHub Release, and pushes images with that same tag. Existing container image names may retain historical naming. Check docker-compose.prebuilt.yml before changing registry, namespace, or tag.

## QQ Music Authorization

The **Version & updates** settings page displays frontend/backend build versions and checks stable releases. Linux/systemd hosts using this repository's GHCR prebuilt Compose deployment can enable one-click updates after installing the host updater. See [update setup and recovery](docs/UPDATING.md).

Playback URLs and user playlists normally require a valid QQ Music login state. Administrators can scan a QR code in the Web console or use:

- GET /admin/qqmusic/status
- POST /admin/qqmusic/cookie
- DELETE /admin/qqmusic/cookie
- POST /admin/qqmusic/qr/confirm

In TeamSpeak chat, `点歌 <song_mid|keywords>` places the matched QQ Music song next in the queue without interrupting the current track. Use `add` to append normally.

Copyright restrictions, VIP-only tracks, and expired Cookies can make a song unplayable. The bot skips an unplayable queue item and continues with the next item.

## External API

Set TSBOT_API_TOKEN or TSBOT_API_TOKENS to protect the external API. Use either an Authorization Bearer token or an x-api-token header.

- GET /external/status
- GET /external/search?source=qqmusic&keywords=Rice Field
- POST /external/queue
- GET /external/history
- POST /external/history/{history_id}/replay

Use the live OpenAPI document as the authoritative API reference.

## Build and Test

~~~bash
make backend-setup
npm --prefix web ci && npm --prefix web run build
cargo build --manifest-path voice-service/Cargo.toml
backend/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~

## Upgrade Notes

On startup, the backend removes queued entries from retired sources so they cannot block playback after an upgrade. Old history entries from retired sources cannot be replayed and may be removed when no longer needed.

## Security

- Never commit tsbot.env, Cookies, tokens, databases, or logs.
- Treat administrator credentials and the initial-password file as sensitive.
- Protect public backend deployments with API tokens, a reverse proxy, and firewall rules.
