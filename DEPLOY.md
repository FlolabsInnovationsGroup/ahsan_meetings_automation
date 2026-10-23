# Deploying Highlight Cutter on AWS

One EC2 instance running two containers:

```
Internet ──HTTPS──> Caddy (TLS certificate + password) ──> app (FastAPI, 1 worker)
                                                            │
                         EBS disk: data/ (videos, jobs) ────┘
                         Docker volume: model cache (WhisperX / pyannote weights)
```

No database is needed: job state is JSON files under `data/jobs/`. Only the
Gemini API (and YouTube, for URL jobs) is called from outside.

## What you need before starting

- An AWS account with permission to create EC2 instances, security groups and Elastic IPs.
- An LLM key for the keep/cut decisions: `ANTHROPIC_API_KEY` (default,
  `LLM_PROVIDER=anthropic`) or `GEMINI_API_KEY` (`LLM_PROVIDER=gemini`).
- `HF_TOKEN` (only if you will process recordings **without** an attached
  `.vtt`/`.srt` transcript; see README for the 3 gated models to accept).
- A domain or subdomain you can point at the server (for example
  `cutter.flolabsinnovations.com`). No domain? Use the free `sslip.io` name
  for the server's IP (step 6).

## 1. Budget alarm (do this first)

AWS console > **Billing and Cost Management > Budgets > Create budget** >
"Monthly cost budget", set an amount, add your email. You get an email
before the bill surprises anyone.

## 2. Launch the instance

AWS console > **EC2 > Launch instance**:

| Setting | Value |
|---|---|
| Region | `eu-central-1` (Frankfurt) or whichever region the team already uses |
| Name | `highlight-cutter` |
| AMI | Ubuntu Server 24.04 LTS (x86_64) |
| Instance type | `t3.large` (2 vCPU, 8 GB RAM). `t3.medium` (4 GB) also works: the setup script adds swap, WhisperX just runs slower |
| Key pair | Create one, download the `.pem`, keep it safe |
| Storage | 30 GB **gp3**. Recordings are deleted once published, so the disk only holds the system, the Docker image, model weights and the meetings currently in progress. EBS volumes can be enlarged later without downtime, never shrunk |

Network settings > **Create security group** with these inbound rules:

| Type | Port | Source |
|---|---|---|
| SSH | 22 | **My IP** only (never 0.0.0.0/0) |
| HTTP | 80 | Anywhere (needed for the certificate and the redirect to HTTPS) |
| HTTPS | 443 | Anywhere |

Note on `t3` instances: they are "burstable". Jobs that run WhisperX use a lot
of CPU for long stretches, which on `t3` costs extra CPU credits. If you
mostly upload Zoom recordings **with** their `.vtt` transcript, WhisperX is
skipped and CPU use stays low. If most jobs need WhisperX, consider `m7i.large`.

## 3. Give it a fixed IP

EC2 > **Elastic IPs > Allocate** > then **Associate** it with the instance.
Without this the IP changes every time the instance stops.

## 4. Point the domain

At your DNS provider, add an **A record**: `cutter` > the Elastic IP.
Wait until `nslookup cutter.yourdomain.com` returns that IP.

## 5. Install

The repo is private, so the server needs its own read-only key
(a GitHub "deploy key"; it can read this one repo and nothing else):

```bash
ssh -i your-key.pem ubuntu@ELASTIC_IP
ssh-keygen -t ed25519 -C "highlight-cutter-ec2" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

On GitHub: repo **Settings > Deploy keys > Add deploy key**, paste the
output, leave "Allow write access" **unchecked**. Then on the server:

```bash
git clone git@github.com:FlolabsInnovationsGroup/ahsan_meetings_automation.git
cd ahsan_meetings_automation
sudo bash deploy/setup-ec2.sh
exit            # log out and back in so docker works without sudo
```

`setup-ec2.sh` installs Docker, adds swap if RAM < 8 GB, creates `data/`
owned by the container user, schedules a daily cleanup of anything older
than 3 days that was never published (`sudo RETENTION_DAYS=7 bash
deploy/setup-ec2.sh` to change it), and turns on automatic security updates.

## 6. Configure secrets

```bash
ssh -i your-key.pem ubuntu@ELASTIC_IP
cd ahsan_meetings_automation

cp .env.example .env
nano .env                       # LLM_PROVIDER + its key, HF_TOKEN, YouTube settings. Keep FFMPEG_BIN=ffmpeg
chmod 600 .env

cp deploy/caddy.env.example deploy/caddy.env
docker run --rm caddy:2 caddy hash-password --plaintext 'CHOOSE-A-STRONG-PASSWORD'
nano deploy/caddy.env           # DOMAIN, BASIC_AUTH_USER, paste the hash in single quotes
chmod 600 deploy/caddy.env
```

No domain yet: set `DOMAIN` to the IP with dashes plus `.sslip.io`, e.g.
`3-121-45-67.sslip.io`. Caddy still gets a real HTTPS certificate for it.

`.env` and `deploy/caddy.env` are gitignored. Never commit them.

## 7. Start

```bash
docker compose up -d --build    # first build: roughly 10-20 minutes
docker compose ps               # app should become "healthy"
docker compose logs -f caddy    # watch for "certificate obtained successfully"
```

Open `https://YOUR_DOMAIN`, log in with the user and password from step 6.

The first job that runs WhisperX downloads 1-2 GB of model weights; later
jobs reuse them from the `model-cache` volume.

## Publishing to YouTube

When a cut is done, the page shows **Publish to YouTube**: title,
description, visibility (Private by default), and "Remove from server after
upload" (on by default). After a successful upload the recording, the cut
and the transcripts are deleted from the server; only the small job record
with the YouTube link stays.

The server uploads with its own long-lived access to the company channel,
set up once. Anyone who knows the site password can publish to the channel,
so share that password only with people allowed to post.

### One-time setup (about 15 minutes)

Use the Google account that owns or manages the company YouTube channel.

1. **Google Cloud project.** console.cloud.google.com > project picker >
   **New project**, name it `flolabs-highlight-cutter`.
2. **Enable the API.** APIs & Services > Library > **YouTube Data API v3**
   > Enable.
3. **Consent screen.** Google Auth Platform (or "OAuth consent screen"):
   - If the channel's Google account belongs to your Google Workspace
     domain, choose **Internal**. No Google review needed.
   - Otherwise choose **External**, then set the publishing status to
     **In production**. Leave it in "Testing" and the connection silently
     expires every 7 days. Google shows an "unverified app" warning during
     the one-time sign-in; continue via "Advanced".
4. **OAuth client.** Credentials > Create credentials > **OAuth client ID**
   > Application type **Web application** > Authorized redirect URIs:
   `https://YOUR_DOMAIN/youtube/callback`. Copy the client ID and secret.
   Google needs a real domain here; an `sslip.io` name may be rejected.
5. **Server.** Add to `.env` and recreate the app container:
   ```bash
   YOUTUBE_CLIENT_ID=...apps.googleusercontent.com
   YOUTUBE_CLIENT_SECRET=...
   PUBLIC_BASE_URL=https://YOUR_DOMAIN
   ```
   ```bash
   docker compose up -d
   ```
6. **Connect.** Open the site, finish a cut, click **Connect YouTube
   channel**, sign in, pick the company channel if Google asks which one,
   and allow access. The page shows "Channel: ..." when it worked.

The access token is stored in `data/youtube_token.json` (readable only by
the app). "Disconnect" on the page removes it.

### Limits to know

- Google's quota allows roughly 100 uploads per day per project, plenty
  for meetings.
- Google used to lock videos uploaded through unaudited API projects to
  Private. Its current documentation says this no longer applies. Check the
  first upload: if YouTube Studio shows it locked to Private, request an API
  audit for the project (YouTube API Services > audit form).
- If uploads start failing with "connection expired", reconnect the channel
  from the page.

## Importing from Zoom

The "From Zoom" tab lists the cloud recordings of the company Zoom account and
imports one with its Zoom transcript (no WhisperX, low CPU).

1. marketplace.zoom.us > Develop > **Server-to-Server OAuth** app, activated.
2. Scopes: `cloud_recording:read:list_user_recordings:admin` and
   `cloud_recording:read:recording:admin`.
3. In `.env`: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`.
   Optional `ZOOM_USER` (default `me`, the account owner) to list another
   user's recordings by email.
4. Check it from the repo folder: `python3 deploy/zoom-check.py`, then
   `docker compose up -d` to load the new values.

Only meetings with a finished MP4 and a transcript can be picked. Turn on
**Create audio transcript** under Zoom Settings > Recording > Cloud recording so new meetings get one.
Nothing is deleted from Zoom; the copy on this server is removed after
publishing, like any other job.

## Calling it from n8n or scripts

Same API as in the README, plus the password:

```bash
curl -u flolabs:PASSWORD -F file=@meeting.mp4 -F transcript=@meeting.vtt \
     -F mode=crosstalk https://YOUR_DOMAIN/jobs
```

In n8n: HTTP Request node > Authentication > Basic Auth.

## Day-to-day operations

| Task | Command (in the repo folder on the server) |
|---|---|
| Deploy a new version | `git pull && docker compose up -d --build` |
| App logs | `docker compose logs -f app` |
| Restart | `docker compose restart app` |
| Disk usage | `df -h / && du -sh data/*` |
| Cleanup log | `cat /var/log/highlight-cutter-cleanup.log` |

Restarting or redeploying while a job is running abandons that job. On
startup the app marks such jobs as failed ("Interrupted by a server
restart"), so redeploy when nobody is processing a recording.

## Known limits of this setup

- **One job at a time is the realistic capacity.** Jobs run inside the API
  process (single worker). Several large WhisperX jobs at once can run out
  of memory. Moving to a real queue (Redis + worker) is the next step if
  usage grows; see section 7.4 of `Highlight_Cutter_System_Report.pdf`.
- **YouTube URLs may fail from AWS.** YouTube often blocks downloads from
  data-center IPs. Uploading files is not affected.
- **One shared password.** Fine for an internal tool. Per-user accounts
  would need an auth layer in the app or in front of it.
- **No backups of `data/`.** By design: recordings live on YouTube once
  published and are removed from the server. If the YouTube token file is
  lost, just reconnect the channel.
