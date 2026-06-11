# MediaGrab

Self-hosted downloader website. Paste a YouTube / TikTok / Instagram / Reddit /
Twitter(X) / most-anything URL, pick **MP4 (max resolution)** or **MP3**, and the
file downloads straight to whatever PC opened the page. Runs yt-dlp + ffmpeg
behind a small Flask web UI, packaged for Docker.

- Max-resolution MP4 by default (or cap at 4K/1440/1080/720/480; optional
  "prefer H.264" toggle for picky players)
- MP3 at best VBR quality with embedded cover art + metadata
- Multiple simultaneous downloads with live progress, cancel, error logs
- yt-dlp self-updates on every container start (sites break old versions fast)
- Files are scratch-only on the server: auto-deleted after `CLEANUP_HOURS` (default 3)
- Deno is baked in — yt-dlp requires an external JS runtime for full YouTube
  support since 2025.11.12

## Quick start — any computer with Docker

Works the same on any Linux box, or Windows/Mac with
[Docker Desktop](https://www.docker.com/products/docker-desktop/) installed:

```bash
# unzip / copy this folder anywhere, then from inside it:
docker compose up -d --build
```

Open `http://localhost:8080` (or `http://<that-machine's-IP>:8080` from
another PC on the LAN). That's it.

## Deploy on Proxmox (Docker LXC)

### 1. Create the Docker LXC (skip if you already have one)

On the **Proxmox host** shell, the community-scripts helper builds a
Debian LXC with Docker preinstalled:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/docker.sh)"
```

Defaults are 2 vCPU / 2 GB RAM / **4 GB disk** — choose *Advanced* settings and
give it **16–32 GB disk** instead; 4K videos need scratch space (a long 4K video
can be 10+ GB during merge).

<details>
<summary>Manual route (no helper script)</summary>

Create an unprivileged Debian 12/13 LXC with **Options → Features →
nesting=1, keyctl=1**, 2 cores / 2 GB / 16+ GB disk, start it, then inside:

```bash
apt update && apt install -y curl
curl -fsSL https://get.docker.com | sh
```

Note: if the LXC rootfs lives on ZFS *directory* storage, Docker's overlay2
may fail — put the rootfs on local-lvm/ext4, or enable `fuse=1` and use
fuse-overlayfs.
</details>

### 2. Copy this folder into the LXC and start it

From this machine:

```bash
scp -r ~/Desktop/mediagrab root@<LXC-IP>:/opt/
```

Inside the LXC:

```bash
cd /opt/mediagrab
docker compose up -d --build
```

### 3. Use it

Open `http://<LXC-IP>:8080` from any PC on the LAN. Paste a link, hit **Grab**,
and the file lands in that PC's browser Downloads folder (auto-download is on
by default; there's also a Download button per job).

## Logged-in / age-gated content (Instagram private posts, etc.)

Public posts on the big sites generally work out of the box. For anything that
needs a login: export a Netscape-format `cookies.txt` from your browser (e.g.
the "Get cookies.txt LOCALLY" extension) into `config/cookies.txt`, then
`docker compose restart`. The footer of the UI shows whether cookies are loaded.

## Configuration (docker-compose.yml)

| Env | Default | Meaning |
|---|---|---|
| `AUTO_UPDATE_YTDLP` | `true` | `pip install -U yt-dlp` on every container start |
| `CLEANUP_HOURS` | `3` | delete finished/failed jobs + files after this many hours |
| `MAX_CONCURRENT` | `3` | simultaneous yt-dlp processes; extra jobs queue |

## Updating / maintenance

- **yt-dlp stopped working for some site** → `docker compose restart`
  (pulls the newest yt-dlp on boot). If that's not enough:
  `docker compose up -d --build --pull always`.
- Job list is in-memory: a restart clears the queue and scratch files.

## Security note

There is **no authentication** — this is built for LAN use behind your
firewall. Don't port-forward it to the internet; anyone who finds it could
download through your IP. If you ever want it remote, put it behind
Tailscale/WireGuard or a reverse proxy with auth.

## Troubleshooting

- **YouTube says "Sign in to confirm you're not a bot"** — your LXC's IP got
  flagged (common on VPNs/CGNAT). Add a `config/cookies.txt` from a logged-in
  YouTube session.
- **A site fails with extractor errors** — restart the container to update
  yt-dlp first; that fixes most breakage.
- **MP4 vs MKV** — the app asks yt-dlp to merge into MP4 and picks AAC audio
  first so that nearly always works; the rare site whose top format can't be
  remuxed will surface an error log in the UI.

## How it works

`Flask (single worker) → job queue (threads) → yt-dlp subprocess per job →
per-job scratch dir in /data → browser fetches the finished file → janitor
thread purges old jobs.` Frontend is one static HTML file polling
`/api/jobs`. Alternative if you ever want a maintained off-the-shelf option:
[MeTube](https://github.com/alexta69/metube) — same idea, fewer knobs.
