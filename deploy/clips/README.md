# Running clips on its own server

Clips cuts Shorts and clips from sources we have the right to use. A person approves them. They post to a separate YouTube channel. It never shares a server, data folder, Google account or channel with the company app.

## 1. A new Google account
Make a new Google account. Give it no role on the company channel. Do not sign in to it in the browser you use for company work.

## 2. The channel
Sign in with that account, create the clips channel, and verify it (phone number). Verification lets you upload videos over 15 minutes. Note the channel id (starts with UC) from YouTube Studio, under Settings, Channel, Advanced.

## 3. A Google Cloud project
In the same account, create a new Google Cloud project. Enable the YouTube Data API v3. Create an OAuth client (type: Web application). Add this exact redirect address: <PUBLIC_BASE_URL>/youtube/callback, where PUBLIC_BASE_URL is the address you open the page at (over the SSH tunnel in step 8 that is http://localhost:8091/youtube/callback). It must match PUBLIC_BASE_URL and CLIPS_PUBLIC_BASE_URL in .env.clips, or Google answers redirect_uri_mismatch. Copy the client id and secret into .env.clips later.

On the OAuth consent screen, publish the app (set it to In production). Do not leave it in Testing. In Testing, sign-in tokens expire after 7 days and uploads stop. Google may show an "unverified app" warning for your own account; you can click through it.

New API projects may be limited to private uploads until Google's audit passes. Check the first uploads in YouTube Studio.

## 4. A small server
Use a separate server from production. Do not put clips on the production server: its restarts and memory limits would clash.

- 2 vCPU and 4 GB memory, about $24 a month (for example a DigitalOcean droplet). Enough for Shorts.
- 4 vCPU and 8 GB, about $48 a month, if you want richer overlays or many long clips.
- 30 GB disk or more.

Cheaper hosts quoted in the research were not available month to month, so plan on these prices. Install Docker and Docker Compose, and clone the clips branch of this repo (the clips code and deploy/clips live on branch clp until it is merged):

    git clone -b clp <the repo address> clips && cd clips

## 5. The settings file
In the repo root on the server:

    cp deploy/clips/env.example .env.clips

Fill in the keys. Set CLIPS_FORBIDDEN_CHANNEL_IDS to the company channel's id. Set CLIPS_CHANNEL_ID to the clips channel id. Set CLIPS_API_TOKEN to a long random string (required: with no key the page only answers when CLIPS_PUBLIC_BASE_URL is localhost, and the API refuses everything else). Keep the file private (chmod 600).

## 6. Initialise (before the first start)

    docker compose -f deploy/clips/docker-compose.yml build
    docker compose -f deploy/clips/docker-compose.yml run --rm web python -m clips init

This makes the data folder and its marker file. The system refuses to run without it, so the loop would exit and restart over and over if it started first.

## 7. Start it

    docker compose -f deploy/clips/docker-compose.yml up -d

Two services start: web (the page, on 127.0.0.1:8091) and loop (the worker). `docker compose -f deploy/clips/docker-compose.yml ps` shows web healthy once the page answers, and loop healthy while it has ticked in the last few loop turns.

To update later (new code from the repo):

    git pull && docker compose -f deploy/clips/docker-compose.yml up -d --build

## 8. Open the page
The page listens on the server's localhost only. Use an SSH tunnel:

    ssh -L 8091:127.0.0.1:8091 user@your-server

Then open http://localhost:8091. A reverse proxy with HTTPS also works.

## 9. Connect the channel
On the page, choose Connect channel and sign in with the new Google account. Check that the channel shown is the clips channel.

## 10. Add the first source
Add a source with its rights record: the basis (own footage, written permission, public permission, clipping campaign, Creative Commons, official material) and the evidence link or note. A source with no basis can be listed but is never clipped or posted. There is no override. Enable it once the rights record is saved.

### Where the videos come from, and the release date
YouTube downloads are off by default (CLIPS_YOUTUBE_DOWNLOADS=false). YouTube's terms forbid downloading a video without YouTube's own written permission, even when the creator agrees, so the normal supply is a watched folder (see below): drop in our own recordings or files a creator sent us. YouTube sources stay listed on the page but are not read or downloaded; turning the setting on is the owner's decision.

The containers see one watched folder, `/clips-inbox` (read only). On the server it is the `clips-inbox` folder in the repo root, next to `deploy/`. Create it before the first start and copy recordings into it:

    mkdir -p clips-inbox
    scp my-recording.mp4 user@server:/path/to/repo/clips-inbox/      # or rsync

Add the folder source with the folder `/clips-inbox`, never the host path: a path that does not exist inside the container cannot be enabled. A file's modified time must be on or after CLIPS_NOT_BEFORE, or it is skipped.

Nothing dated before CLIPS_NOT_BEFORE (default 2026-11-19, the game's release date) is clipped, unless its source is official material. A folder file is dated by its modified time. A video with no known date passes. Set it empty to switch the gate off; a value that is not a date (write YYYY-MM-DD) stops watching and says so on the page.

Nothing is ever public by accident: every upload is private with a publish time at least 15 minutes ahead. A clip whose slot has passed or is under 15 minutes away goes back to approved and gets a new slot.

## 11. The daily routine (about ten minutes)
1. Open the page.
2. Watch each new draft clip. Approve it or reject it with a reason.
3. Glance at the event log for errors or a held clip.

Approved clips get the next free slot. They upload private with a publish time, and YouTube releases them.

## 12. The kill switch
Use the Stop button on the page, or create an empty file named KILL in the data folder:

    docker compose -f deploy/clips/docker-compose.yml exec web touch /data-clips/KILL

Nothing is fetched, rendered or uploaded while it is on. Delete the file (or use the page) to resume.

Videos already uploaded (private, waiting for their publish time) would still go public by themselves. So while the switch is on, every loop turn sets them back to private (and the button and `python -m clips kill` do it at once). A clip that is mid-upload when you press Stop is set back to private as soon as it finishes. Uploads go up at most CLIPS_UPLOAD_AHEAD_HOURS (6) before release, which limits what can be waiting. If the page or the command line says a video could NOT be set back to private, open YouTube Studio and set it to private by hand, now. This only works in a process started on the clips folder; from anywhere else nothing is touched and you are told.

## 13. Raising the posting pace
Edit .env.clips and restart. The default starts at 1 a day and climbs slowly (CLIPS_RAMP_STEPS: 1, 2, 2, 3, 3, 3, 3, 4 a day by week) up to the cap of 6, at least 3 hours apart. Change CLIPS_POSTS_PER_DAY, CLIPS_RAMP_STEPS (or CLIPS_RAMP_PER_WEEK), CLIPS_MAX_POSTS_PER_DAY and CLIPS_MIN_GAP_MINUTES. Hourly posting is possible (gap 60) but hurts a new channel. Go up slowly and watch views.

    docker compose -f deploy/clips/docker-compose.yml up -d

## 14. A copyright claim or strike
1. Turn the kill switch on.
2. Open YouTube Studio and read the claim. Note which clip and which source.
3. Remove the clip if the claim is valid. Check the source's rights record: is the evidence real and current?
4. If it was a strike, the breaker opens at once and uploads stop. Do not reset it until you know why it happened.
5. Fix the rights record or disable the source. Then reset the breaker from the page and turn the kill switch off.

## 15. Backups
All state is in the clips-data volume, mounted at /data-clips (database, sign-in token, upload journal, renders). Back up the database, the token and the upload journal (state/, the record that stops a clip from being posted twice). Weekly:

    docker compose -f deploy/clips/docker-compose.yml exec web python -c "import sqlite3; sqlite3.connect('/data-clips/clips.sqlite').backup(sqlite3.connect('/tmp/clips.sqlite'))"
    docker compose -f deploy/clips/docker-compose.yml exec web sh -c "tar czf /tmp/clips-backup.tgz -C /tmp clips.sqlite -C /data-clips youtube_token.json state"
    docker compose -f deploy/clips/docker-compose.yml cp web:/tmp/clips-backup.tgz ./clips-backup.tgz

The first command takes a safe copy of the live database (a plain file copy of a running SQLite database can miss its latest writes). The archive is made outside the volume and copied off the server; keep it somewhere private, since it holds the sign-in token.

Rendered files can be re-made. Downloaded media is deleted after 48 hours by design. Rendered clips are deleted a day after a rejection and a few days after a post.

## 16. When something is held or waiting
- Uploads wait (the page's event log says "uploads are waiting") while no channel is connected, the data folder is wrong, or the connected channel is not the clips channel. No clip is changed. Fix the cause and the next turn goes on.
- A clip is HELD when a check is about its own rights or file. Fix the source's rights record, then press Release on the Schedule table (or `python -m clips release <id>`): it goes back to approved and gets a new slot.
- A scheduled time that passes while posting was stopped is booked again. Nothing is posted late and all at once.
