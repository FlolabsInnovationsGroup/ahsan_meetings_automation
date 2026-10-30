# Deploying Highlight Cutter on AWS

One EC2 instance running two containers:

```
Internet ──HTTPS──> Caddy (TLS certificate + password) ──> app (FastAPI, 1 worker)
                                                            │
                         EBS disk: data/ (videos, jobs) ────┘
                         Docker volume: model cache (WhisperX / pyannote weights)
```

No database server is needed: job state is JSON files under `data/jobs/`,
and what must outlive the jobs (the list of videos on YouTube, the uploads in
flight, the autopilot's meetings, the Zoom account list) is small text files
under `data/state/`, copied to `data/state-backup/` after every change and to
S3 once a day (see "Autopilot").

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
| SSH | 22 | **My IP** to start (see "Team SSH access" to open it safely for a team) |
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

When a cut is done, the page shows **Publish to YouTube**. Choose the full
video, any of its shorts, or both; edit each title and description; pick the
visibility (Private by default). The shorts go up first, each saying where
its clip plays in the full video ("This clip starts at 01:50 in the full
video."), then the full video goes up with its chapters and a list of the
shorts with links. Shorts can also be posted alone.

"Remove the files from the server once everything is published" is on by
default: after the full video and every short are on the channel, the
recording, the cut, the shorts, the zip and the transcripts are deleted from
the server; only the small job record with the YouTube links stays. Nothing is
deleted while something is still to post or failed.

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

- Google's quota allows roughly 100 uploads per day per project. A meeting is
  one upload for the full video plus one per short (4 by default), plenty.
- A short is an ordinary upload: YouTube treats a video as a Short when it is
  vertical or square and up to 3 minutes, and ours are 1080x1920 and at most
  60 seconds. Nothing else marks it.
- The app only asks Google for permission to upload (and to read the channel's
  name), so it cannot edit a video after uploading it. That is why the shorts
  go first: the full video's description is written once, with their links in
  it. A short posted after the full video is not in that list; the page shows
  the list to paste into the description in YouTube Studio.
- Links in a Short's own description are not clickable (YouTube turned that
  off in 2023). To give a Short a clickable link, set its "Related video" to
  the full video in YouTube Studio; the API cannot do that.
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
2. Scopes: `cloud_recording:read:list_user_recordings:admin`,
   `cloud_recording:read:list_recording_files:admin` and
   `cloud_recording:read:recording:admin`; for the autopilot also
   `meeting:read:list_meetings:admin` (it reads the meeting schedule).
3. In `.env`: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`.
4. Check it from the repo folder: `python3 deploy/zoom-check.py`, then
   `docker compose up -d` to load the new values.

The Zoom tab's dropdown lists the accounts (PM, `ZOOM_DEFAULT_EMAIL`, is the
default; "Add a new email…" adds one for everyone). A meeting already on
YouTube has a green Process button with "Posted" (or "Partly posted" when
only some shorts are) under it. Turn on **Create audio transcript** under
Zoom Settings > Recording > Cloud recording so new meetings get one.
Nothing is deleted from Zoom; the copy on this server is removed after
publishing, like any other job.

## Autopilot

Every new recording of the accounts ticked **Autopilot** under the Zoom tab
(PM by default) is processed and posted without anyone pressing a button:

1. It waits for Zoom's own transcript, up to `AUTOPILOT_TRANSCRIPT_WAIT_HOURS`
   (10) after the meeting ends; after that the server transcribes it with
   WhisperX (needs `HF_TOKEN`; such a job may run `ASR_JOB_MAX_WALL_S`, 6 h).
2. It processes one meeting at a time, and never while another job runs.
3. It posts the shorts, then the full video (listing the shorts), as
   `AUTOPILOT_PRIVACY` (public), with the suggested titles and descriptions,
   then removes the files from the server.
4. A failed meeting is tried again after about 30 min, 2 h and 6 h, then
   shows "needs attention" in the side panel with a Retry button.

It finds new recordings three ways: Zoom's webhook (at once), Zoom's meeting
schedule (15 minutes after a scheduled meeting ends) and a look every
`AUTOPILOT_CHECK_MINUTES` (30) while no job runs. Meetings recorded before the
day it was switched on (or `AUTOPILOT_START_AFTER`) are never posted by it.
Setting `AUTOPILOT_START_AFTER` to a past day posts the older meetings too:
look on the channel first, because the app only knows videos whose job
records were still on the server when this version started.

**Nothing is posted twice.** Every upload is written down before it starts
(`data/state/uploads.jsonl`), with YouTube's upload link before the first
byte; one link can make only one video, so after a crash the upload is
finished through the same link instead of being sent again. A meeting has one
full video, and all its shorts come from one processing run. When the app
really can't tell whether a cut-off upload reached YouTube, it stops and asks
in the side panel ("It's on YouTube" / "Not there, upload again"). Pressing
Publish by hand goes through the same list, so a person can't double-post
either. `data/state/posted.jsonl` is the list of everything on the channel
(one line per video, readable in any text editor).

Turning it on:

1. In the Zoom app: add the `meeting:read:list_meetings:admin` scope. Under
   Feature > **Event Subscriptions** start a subscription with the endpoint
   `https://YOUR_DOMAIN/zoom/webhook` and the events "All recordings have
   completed" and "Recording transcript files have completed", and copy the
   app's **Secret Token** (don't press Validate yet).
2. In `.env`: `ZOOM_WEBHOOK_SECRET_TOKEN` (that token) and `HF_TOKEN` (for
   WhisperX). Leave
   `REQUIRE_NATIVE_TRANSCRIPT=true`: it still applies to jobs started by
   hand; the autopilot's own jobs may transcribe.
3. Load it: `docker compose up -d` (a plain `restart` doesn't re-read
   `.env`), and once, so Caddy lets Zoom reach the webhook without the site
   password: `docker compose up -d --force-recreate --no-deps caddy` (later
   updates do this by themselves when the Caddyfile changes).
4. Now press **Validate** in Zoom and save the subscription.
5. Press **Turn on** in the page's side panel (it asks to confirm). It then
   says "Autopilot is on", with the accounts it watches; Pause and Turn off
   are there too. The switch is saved on the server, so it stays on across
   restarts and updates.

Videos published before this version are added to the posted list at
startup from their job records; any whose job folder the daily cleanup had
already removed are not known to the app (look at the channel before
processing such an old meeting again).

Daily copy to S3 (optional, recommended): create a bucket, give the EC2
instance an IAM role allowing `s3:PutObject` on it, set
`STATE_BACKUP_S3_URI=s3://BUCKET/highlight-cutter` in `.env`, and re-run
`sudo bash deploy/setup-ec2.sh` once (it installs the AWS CLI and the cron
job). Test it with `bash deploy/backup-state.sh`.

## Calling it from n8n or scripts

Same API as in the README, plus the password:

```bash
curl -u flolabs:PASSWORD -F file=@meeting.mp4 -F transcript=@meeting.vtt \
     -F mode=crosstalk https://YOUR_DOMAIN/jobs
```

In n8n: HTTP Request node > Authentication > Basic Auth.

## Automatic updates from GitHub

Every push to `main` updates the server once the tests pass: the `deploy` job
in `.github/workflows/tests.yml` SSHes in and runs `deploy/auto-update.sh`,
which waits (up to 45 min) for running jobs and YouTube uploads to finish,
pulls, rebuilds and waits for the app to be healthy. Log:
`~/ahsan_meetings_automation/auto-update.log`. The GitHub key is locked to
that script (`restrict,command=...`), so it can't open a shell.

One-time setup on the server:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions" -f ~/.ssh/github_actions_deploy
echo "restrict,command=\"$HOME/ahsan_meetings_automation/deploy/auto-update.sh\" $(cat ~/.ssh/github_actions_deploy.pub)" >> ~/.ssh/authorized_keys
echo "===== DEPLOY_SSH_KEY ====="; base64 -w0 ~/.ssh/github_actions_deploy; echo
echo "===== DEPLOY_HOST_KEY ====="; cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub
```

Then in GitHub > Settings > Secrets and variables > Actions, add
`DEPLOY_SSH_KEY` (the one long base64 line) and
`DEPLOY_HOST_KEY` (the `ssh-ed25519 ...` line). Afterwards delete the private
key from the server: `rm ~/.ssh/github_actions_deploy`.

Turn it off by deleting the `DEPLOY_SSH_KEY` secret, or on the server:
`sed -i '/ github-actions$/d' ~/.ssh/authorized_keys`. Update by hand any time
with `bash deploy/auto-update.sh`.

## Team SSH access

To let people in from anywhere without chasing their changing IPs, open SSH to
everyone and rely on keys: the server accepts no passwords. Never share the
`.pem`; each person gets their own key so access can be removed one by one.

Once:

1. On the server first (setup-ec2.sh turns off passwords and root login and
   installs fail2ban; safe to re-run):

```bash
cd ~/ahsan_meetings_automation && git pull
sudo bash deploy/setup-ec2.sh
sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '   # all "no"
sudo fail2ban-client status sshd
```

2. Only then: EC2 > Security Groups > inbound SSH rule > Source **Anywhere-IPv4**.

Per person: they run `ssh-keygen -t ed25519 -C "<name>"` and send you the
`~/.ssh/id_ed25519.pub` line (public, safe to send). You add it with
`echo "<line>" >> ~/.ssh/authorized_keys`; they connect with
`ssh ubuntu@<ELASTIC_IP>`. List access: `cut -d' ' -f3- ~/.ssh/authorized_keys`.
Remove someone: `sed -i '/ <name>$/d' ~/.ssh/authorized_keys`.

## Day-to-day operations

| Task | Command (in the repo folder on the server) |
|---|---|
| Deploy a new version | automatic after each push to main; by hand: `bash deploy/auto-update.sh` |
| App logs | `docker compose logs -f app` |
| Restart | `docker compose restart app` |
| Disk usage | `df -h / && du -sh data/*` |
| Free space (each build leaves GBs of cache) | `docker image prune -f && docker builder prune -f && df -h /` |
| Cleanup log | `cat /var/log/highlight-cutter-cleanup.log` |
| What is on YouTube | `cat data/state/posted.jsonl` |
| S3 backup log | `cat /var/log/highlight-cutter-backup.log` |
| Which ffmpeg the app uses (must say 9.0.1) | `docker compose exec app ffmpeg -version \| head -1` |

The image runs a pinned ffmpeg 9.0.1, the version the renderer is tested on;
Debian's own ffmpeg stays installed only for WhisperX's libraries. To upgrade
ffmpeg, change the tag and digest on the Dockerfile's `AS ffmpeg` line (CI
then runs the render tests on the new build before it is deployed).

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
- **Only `data/state/` is backed up.** Recordings live on YouTube once
  published and are removed from the server. `data/state/` (what is on
  YouTube, uploads in flight, the autopilot's meetings) is copied to
  `data/state-backup/` after every change and to S3 daily. If the YouTube
  token file is lost, just reconnect the channel.
