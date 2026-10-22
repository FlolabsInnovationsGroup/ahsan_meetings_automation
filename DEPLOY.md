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
- `GEMINI_API_KEY`.
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
| Storage | 60 GB **gp3** (videos are large; raise if you keep many) |

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
owned by the container user, schedules a daily cleanup of jobs older than
14 days (`sudo RETENTION_DAYS=30 bash deploy/setup-ec2.sh` to change it),
and turns on automatic security updates.

## 6. Configure secrets

```bash
ssh -i your-key.pem ubuntu@ELASTIC_IP
cd ahsan_meetings_automation

cp .env.example .env
nano .env                       # GEMINI_API_KEY, HF_TOKEN. Keep FFMPEG_BIN=ffmpeg (not a Windows path)
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
- **No backups of `data/`.** Outputs are re-creatable from the original
  recordings and are pruned after the retention window anyway. If you need
  them kept, enable EBS snapshots via Amazon Data Lifecycle Manager.
