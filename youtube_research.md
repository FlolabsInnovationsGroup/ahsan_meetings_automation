# YouTube growth research: progress file

Started 2026-11-03. Goal: find what will bring more traffic to the company
YouTube channel (edited team meetings + 20-60 s Shorts, posted automatically
by Highlight Cutter) and how to build each idea into the app.

If this work stops part-way, a new session can continue from this file:
read it, read the notes folder below, and carry on from "Next steps".

## Status

- [x] Research (5 strands done; notes in `research_notes/YouTube growth for meeting channel/`)
- [x] Final report: `reports/YouTube growth for meeting channel.md` (about 5,800 words)
- [x] Findings summarised in this file (below)
- [x] Plan for building it into the app (below; full detail in the report)

## The 5 research strands

| # | Strand | Notes file |
|---|---|---|
| 1 | Long-form SEO and discovery: titles, descriptions, tags, hashtags, chapters, thumbnails, Test & Compare, captions, translations, playlists, end screens, cards, pinned comments, timing; official vs folklore | `longform_seo_discovery.md` |
| 2 | Shorts: how they are ranked, hooks, length, linking Shorts to the full video, cadence | `shorts_growth.md` |
| 3 | YouTube Data API: what the app can set automatically, quota costs, scopes, the audit for unverified apps, Analytics API for a feedback loop | `youtube_api_capabilities.md` |
| 4 | Strategy for company / engineering channels that post meetings: case studies, packaging, series, cross-promotion, risks (confidential info, privacy) | `b2b_tech_channel_strategy.md` |
| 5 | AI-written titles/descriptions/thumbnails, translations and dubbing, and YouTube's spam / "inauthentic content" / AI-disclosure rules | `ai_automation_policy.md` |

All notes files are in `research_notes/YouTube growth for meeting channel/`.

## What the app does today (for context)

- Uploads the full video and each short with the YouTube Data API, scope
  `youtube.upload` only (publish/youtube.py, publish/run.py).
- Has per meeting: transcript, cleaned transcript, chapters (chapters.txt),
  highlights reel, shorts with AI titles and hooks, removed-parts video,
  report.pdf.
- The full video's description = chapters + links to its shorts; a short's
  description says where it plays in the full video.
- Autopilot posts every new team meeting publicly.

## Findings

### 1. Long-form SEO and discovery (done, notes: longform_seo_discovery.md)

Caveat: the sandbox could not open support.google.com / youtube.com pages
directly; findings come from search results quoting them. Check exact wording
on the live pages before quoting publicly.

- How YouTube ranks (official): search = relevance + engagement + quality,
  weighted per query, and personalised. Relevance reads the title, tags,
  description and the video itself. Recommendations increasingly weigh
  viewer-satisfaction surveys over raw watch time; YouTube "pulls" videos to
  viewers, so the first hours are not make-or-break. Percent weightings in SEO
  blogs have no official source.
- Tags: officially "minimal role" (mainly misspellings). Title, thumbnail and
  description matter more.
- Limits: title 100 characters, description 5,000 (API: bytes), tags 500
  characters total. Hashtags: up to 3 shown by the title; more than 60 and all
  are ignored. About 50-70 title characters show on mobile/search
  (third-party estimate).
- Chapters (official): first at 00:00, at least 3, each at least 10 s; ours
  override automatic ones. Google Search uses description timestamps for
  "key moments": an extra discovery path from Google for long videos.
- Thumbnails (official): 1280x720 (min 640 wide), JPG/GIF/BMP/PNG, under
  2 MB; custom thumbnails need a phone-verified account in good standing,
  with a daily upload cap.
- Test & Compare (official): up to 3 titles and/or thumbnails, winner by
  watch-time share (not CTR); editing mid-test stops it; about 2 weeks. Title
  testing added Dec 9, 2025 (news), needs advanced features. Studio only.
- Translations (official): translated titles/descriptions can appear in
  search for those languages; multi-language audio creators got over 25% of
  watch time from other languages. Auto-dubbing reportedly open to all
  creators since Feb 4, 2026 (27 languages; news, not official).
  Captions helping search ranking: no official source (folklore).
- End screens (official): last 5-20 s, video at least 25 s, up to 4
  elements. One unconfirmed report says now limited to YPP channels.
- Timing/cadence: no official evidence it affects ranking; "wait 24-48 h"
  tactic debunked by YouTube's liaison.
- Long videos: no official penalty for 1-2 hour videos.
- "Inauthentic content" (official, Jul 15 2025): renamed from "repetitious";
  affects monetisation only; examples are near-identical mass-made videos.
  Risk for us: identical templated titles/thumbnails across meetings.
- Gaps: official thumbnail face/text advice; playlist/card/pinned-comment
  limits; whether captions help search; current auto-dubbing eligibility.

### 2. Shorts (done, notes: shorts_growth.md)

Same caveat: official pages were blocked; findings come from search
summaries and secondary coverage.

- How the Shorts feed ranks (official, Creator Insider Aug 2023): the main
  signal is "viewed vs swiped away" ("the audience is the algorithm"). Posting
  more often doesn't raise reach, and many low-quality clips can hurt. Time
  of day doesn't matter (except news). Top Shorts reach 70-90% viewed vs
  swiped (industry, Paddy Galloway).
- Ranking change 1 Oct 2026 (official): an originality update cuts reach for
  re-uploaded content. Claimed April 2026 changes have no official source.
- Views (official, from 31 Mar 2025): a view counts on every play/replay;
  the old count lives on as "engaged views" (used for YPP). Compare our
  Shorts on engaged views.
- Length: up to 3 min since 15 Oct 2024 (official). No official ideal
  length; industry studies disagree (11-20 s vs 40-60 s). Hooks in the first
  1-3 s, bold on-screen text, loopable endings: industry advice only.
- Related video link (official, since Aug/Sep 2023): each Short can link to
  one video on the same channel, set in Studio > Details. NOT settable via the
  public API (industry: OpusClip, Vizard), so it is a manual Studio step.
  Effect: anecdotally low click rate; one vendor claims +10% long-form views,
  best when the Short goes up within 3 days of the long video.
- Links in Shorts descriptions and comments are not clickable since 31 Aug
  2023 (official); use channel profile links.
- A Short = vertical or square, up to 3 min (official). #shorts not needed
  (industry consensus). Hashtags help categorisation.
- Custom thumbnails for Shorts (official, rolled out 24-25 Jul 2026): only
  Partner Program channels, from desktop, no A/B testing. thumbnails.set via
  API: no documented Shorts exclusion, vendors say unreliable; test it.
- Do Shorts help long-form? Mixed: an 18,000-channel study (Mar 2025-Mar
  2026) found channels mixing both usually have more subscribers; an arXiv
  study found long-form views fell for very large channels after starting
  Shorts (correlational). No official figure.
- B2B/tech: few case studies (Lovable's tech-history Shorts); none for
  robotics/IoT/space.
- For our pipeline: start each Short on its strongest line; make the burned-in
  title state the claim or result; track engaged views and swipe-away; set the
  related-video link by hand in Studio; publish only Shorts that pass a
  quality bar; check clips for confidential content (Shorts reach mostly
  non-subscribers).

### 3. YouTube Data API (done, notes: youtube_api_capabilities.md)

Caveat: developers.google.com was blocked; facts come from search summaries
of the official pages; items marked [training knowledge] in the notes are
unconfirmed.

- Quota: videos.insert dropped from ~1,600 to ~100 units (official revision
  history; third parties date it 4 Dec 2025). Uploads/search later moved to
  their own buckets (date unclear, ~100 uploads/day reported). Rest of the
  default 10,000 units/day: captions.insert 400, thumbnails.set 50,
  playlistItems.insert 50, videos.update 50, search.list 100, videos.list 1.
  More quota = audit + extension form (weeks to months).
- Private lock: uploads from unaudited API projects created after
  28 Jul 2020 are forced private. One summary says the current page no
  longer says so: unresolved. App should read back privacyStatus after
  each upload (ours already posts publicly, so probably audited or not
  affected: check).
- Scopes: youtube.upload = insert (+ probably thumbnails.set). Adding
  youtube.force-ssl unlocks videos.update, playlists, captions, comments;
  yt-analytics.readonly unlocks Analytics. Both are sensitive scopes:
  Google OAuth verification with a demo video; the channel must reconnect.
- Settable at upload with today's scope: title (100), description (5,000
  bytes), tags (500), category 28 (Science & Technology), default
  language/audio language, localizations (translated title/description),
  publishAt (scheduling, private only), selfDeclaredMadeForKids,
  containsSyntheticMedia (since 30 Oct 2024), license, embeddable.
- Chapters: only via description timestamps. Thumbnails: limit raised to
  50 MB (14 Sep 2026, official); channel must be phone-verified; on Shorts
  unreliable. Captions: 400 units, force-ssl, upload timed SRT/VTT
  (auto-sync deprecated 13 Mar 2024).
- Studio only (no API): pinning comments, end screens, cards, Shorts related
  video link, Test & Compare, Premieres, auto-chapters toggle.
- Analytics API: views, watch time, avg view duration/percentage, retention,
  traffic sources (YT_SEARCH, SHORTS, ...), top 25 search terms;
  thumbnail impressions/CTR added 15 Jan 2026 (Reporting API "reach").
  View counting changed 27 Aug 2026: before/after not comparable.

### 4. Company / engineering channel strategy (done, notes: b2b_tech_channel_strategy.md)

- Raw meetings get almost no views: Kubernetes SIG meeting recordings
  averaged ~3 views each (2020-21). GitLab Unfiltered (~38K subs) posts
  meetings for hiring/enablement, not reach (sample video 657 views).
- Channels that grow publish deliberate, topic-led shows: Tailscale 1.5K to
  50K subs, 6K to 500K+ monthly views in ~2.5 years with deep technical
  videos and no clickbait (Nov 2025). Oxide and Friends: a weekly named show.
- Packaging: titles name the insight, never "Team Meeting #12"; chapters;
  Google "key moments"; VideoObject/Clip markup on our own web pages.
- Short clips: vidIQ (Jul 2026, 448K new channels) median first Short ~1,362
  views in 30 days vs ~180 for the first long video. Short-to-full-video link
  converts under 1%: clips must stand on their own.
- Search: YouTube is in Google's top 10 for 80% of B2B "demo" searches and
  ~20% of "vs"/"best": evergreen how-to / decision cuts are the most
  dependable early traffic.
- Risk controls: GitLab keeps reviews private and has a takedown runbook.
  Recommended: private by default, transcript + on-screen scan for secrets,
  consent registry, meeting types that never auto-publish, takedown runbook;
  ITAR/EAR export-control check for space/robotics detail (legal review).
- Benchmarks: median B2B channel ~1.5K views and ~8 new subs/month (2023);
  ~1:23 average view duration; good CTR ~4-6% (not B2B-specific).
- Recommended for FloLabs: two tiers (curated episodes + clips on the main
  channel; full raw meetings in an "Unfiltered" channel/playlist); series
  per team ("Robotics Weekly: <insight>"); score transcript moments for clips
  (decision, number, failure/result, stands alone, visual payoff, matches a
  search phrase, confidentiality flags); repurpose each transcript into a
  blog post, newsletter item and LinkedIn/X clips.

### 5. AI metadata, thumbnails, translation and policy (done, notes: ai_automation_policy.md)

- No YouTube rule bans AI-written metadata or automated uploads. Removal risk:
  misleading titles/thumbnails/descriptions (spam & deceptive practices),
  tag lists in descriptions; "egregious clickbait" removals started in India
  Dec 2024. Monetisation risk: "inauthentic content" (15 Jul 2025):
  near-identical, templated, bulk content; reviewers look at titles and
  thumbnails too. For us: avoid identical templates, many near-duplicate
  Shorts from one meeting, dead air, overpromising titles.
- AI disclosure (containsSyntheticMedia) only for realistic altered/synthetic
  content. LLM metadata and real-frame thumbnails: no. Cloned voices or
  realistic AI images of people: yes. Decide it with a code rule.
- LLM vs human titles: no rigorous YouTube study; roughly equal in nearby
  research. Vendor "AI won by 340%" claims are marketing. YouTube's
  Inspiration tab is being replaced by Ask Studio (from Aug 2026).
- Thumbnails: largest vendor dataset (507K videos): text overlays are the
  clearest difference (86.6% of breakout videos). Faces about neutral on
  average. 1-5 words beat 11+. Test & Compare: up to 3, 14 days,
  watch-time share, no API. Notes include an ffmpeg + OpenCV/MediaPipe +
  Pillow frame-picking pipeline sketch.
- Translation: auto-dubbing available to everyone in 27 languages
  (4 Feb 2026); multi-language audio to all creators (Sep 2025): 25%+ of
  watch time from other languages in YouTube's pilot. Translated titles,
  descriptions and captions can be uploaded via the API.
- Prompt design: enforce field limits and chapter rules in code; every claim
  backed by transcript quotes + a verifier pass; pass previous titles so each
  video gets a different structure (avoids the "template" signal); only
  app-supplied links; no tag lists in descriptions.
- Feedback loop: Test & Compare + Analytics API; no tool closes the loop; a
  small channel won't have enough impressions for conclusive per-video tests.

### Caveat for all strands

The sandbox network blocked most official YouTube/Google pages, so findings
come from search summaries and press. Several 2026 dates (quota buckets,
thumbnail size, view-count change) need checking on the live official pages
before we build on them.

## Plan for the app

Ranked by impact against effort (full detail, files and API calls in the
report, section "Ten changes ranked by impact against effort"):

1. Packaging pass (biggest win, no new scope): today the title is
   "<Zoom topic> — <date>" (publish/describe.full_title), the description is
   only chapters, and the only tag is the hidden `hc…` marker. New
   decide/packaging.py + prompt writes packaging.json: title candidates
   backed by transcript quotes, a summary for the description's first lines,
   2-3 hashtags, a few tags; describe.py uses it; upload also sends category
   28, languages, containsSyntheticMedia=false. Keep the `hc…` marker within
   the 500-character tag limit (find_by_marker needs it). Highlight clips can
   become their own chapters.
2. Review window: upload private with publishAt some hours ahead
   (autopilot_publish_delay_hours) + a confidentiality screen
   (decide/screen.py, like content.py).
3. Shorts: a <=40-character hook burned in, the zoom-in crop and HD encode
   (POSSIBLE_FEATURES.md "Next up"), hashtags, optionally one short a day.
4. Studio checklist in the publish panel for what has no API: the Short's
   link to its full video, end screen, pinned comment, Test & Compare.
5. Thumbnails from real frames (slice/thumbnail.py), thumbnails.set (50 units).
6. Reconnect once with youtube.force-ssl: caption upload from
   transcript_clean.json (400 units), fix the full video's shorts list
   automatically, team playlists.
7. Post highlights.mp4 as its own video (new ledger item kind).
8. Analytics loop: yt-analytics.readonly + a daily publish/analytics.py,
   shown in the side panel and fed back into the packaging prompt.
9. Translated titles and descriptions at upload (no extra quota).
10. A blog post per meeting on FloLabs' site with video markup (Google
    "key moments").

Code risks found by the report:
- is_account_wide_error treats any 401/403 as "stop the whole run": a failed
  thumbnail or missing-scope call would halt all uploads. Make them fail soft.
- _load_credentials passes the full SCOPES list, so a token missing a new
  scope only shows up as a 403 later: youtube_status should compare granted
  scopes and ask to reconnect.

Expectations: raw meeting uploads elsewhere get about 3-30 views each;
Tailscale took ~2.5 years to reach 500K monthly views. Quota: an upload is
~100 units now (older pages say 1,600); all additions come to ~800 units
per meeting, inside the 10,000/day default.

Verify on the live official pages before building (table in the report):
quota values, the private-lock rule for unaudited projects, thumbnail size
limit and whether thumbnails.set works with the upload scope, publishAt for
Shorts, Analytics impressions/CTR metric names, 2026 dates (auto-dubbing,
Shorts thumbnails, originality update), end screens limited to YPP?, 7-day
token expiry while the OAuth app is in Testing mode.

## Next steps

1. Owner picks which of the 10 changes to build first (recommended: 1, 2, 3).
2. Verify the facts listed above on the live official pages.
3. Build, test and deploy one change at a time.

---

# Round 2: every traffic method, in depth (started 2026-11-03)

The owner asked for a far more thorough search: every possible way to bring
traffic, with the best methods found and how to build them into the app.
Round 1's report stays as background:
`reports/YouTube growth for meeting channel.md`.

Round 2 notes: `research_notes/YouTube traffic methods in depth/`
Round 2 report: `reports/YouTube traffic methods in depth.md`

## Round 2 strands (8, in parallel)

| # | Strand | Notes file |
|---|---|---|
| 1 | How the algorithm really works: official sources, Google's published papers, how new/small channels get first impressions, suggested-video strategy | `algorithm_deep_dive.md` |
| 2 | Keyword and topic research methods and tools, incl. what can be automated (autocomplete, Trends, Studio Research tab) | `keyword_topic_research.md` |
| 3 | Packaging: title and thumbnail studies, frameworks, what works for technical audiences | `packaging_titles_thumbnails.md` |
| 4 | Retention and structure: hooks, cold opens, pacing, length, podcast/long-conversation channels, YouTube Podcasts | `retention_structure_podcasts.md` |
| 5 | Off-YouTube traffic: Google video SEO, schema, sitemaps, AI search citing videos, LinkedIn/X/Reddit/HN, newsletters, embeds, ads | `offplatform_distribution.md` |
| 6 | Channel features and engagement: channel page, playlists/series, community posts, live/premieres, collabs, Hype, Shorts search/series | `channel_features_engagement.md` |
| 7 | Clipping/repurposing tools (OpusClip, Vizard, Descript, ...) and which of their features we can copy (speaker-tracking crop, animated captions, virality score) | `repurposing_tools.md` |
| 8 | Analytics and experiments: which metrics, benchmarks, Studio Research/content gaps, A/B testing at low traffic, automated insights | `analytics_experiments.md` |

Status: research running.

## Round 2 findings

### R2-3. Titles and thumbnails (done, notes: packaging_titles_thumbnails.md)

- Title studies are observational vendor data on views, not CTR. Length: no
  effect (28,947 and 2,622 videos). Question marks: no advantage (7,935
  million-view videos). Numbers: conflicting. Popular "3M video study",
  "odd numbers +20%", "questions +14% CTR" have no primary data.
- Thumbnails: vidIQ 2026, 500 breakout videos / 30 niches: 28% no text,
  24% 1-3 words, median 5 words when text used; 69% show a face, 89% a
  face or high contrast. Peer-reviewed 2022 (3,745 brand videos): inverted U
  for faces (a moderate number best). "38% more clicks with faces" is a
  2014 Instagram study, not YouTube.
- Frameworks (opinion): Paddy Galloway: titles under 60 characters, under 5
  words on the thumbnail, "glance test", title and thumbnail complement not
  repeat. MrBeast handbook: package sets the viewer's expectation; decide the
  package before production.
- Clickbait: "egregious clickbait" enforcement from Dec 2024 (India first).
  Test & Compare winner = watch-time share, not CTR.
- Test & Compare: thumbnails from Jun 2024, titles ~mid-2025. Made on
  YouTube (23 Sep 2026): dynamic thumbnails rotated per audience segment,
  A/B tests of up to 3 video cuts, Ask Studio title/thumbnail suggestions;
  Shorts cut testing said to come 2027. Low-traffic tests often
  inconclusive.
- Shorts: the 1-2 s swipe decision matters more than the title; on-screen
  hook text of 6 words or fewer in the first 2 s (practitioner advice).
- Pipeline recipe (synthesis, untested) in the notes: frame scoring
  (penalise Zoom grids with more than 2 faces), fixed brand template with
  0-3 words of text, three different angles per video (outcome, problem,
  visual curiosity) for testing, a 6-step checklist ending with "every claim
  maps to a transcript timestamp", a banned-pattern list.

### R2-4. Retention, structure and podcasts (done, notes: retention_structure_podcasts.md)

- First 30 s: Studio's "Intro" key moment = share still watching at 30 s,
  i.e. did the opening match the title/thumbnail. No official retention
  benchmark for long videos; vendor "good" averages 30-50%+ conflict. Firm
  data point: vidIQ's own long videos averaged 30.3% viewed / 3:11
  (mid-2025 to mid-2026), its Shorts 73.6%.
- Highlights first vs chronological, cold opens, zooms, B-roll: no rigorous
  evidence. Our highlights reel helps only if its first seconds deliver what
  the title promises; keep the title card short or lay it over the first
  highlight.
- Big shows: the full episode stays the main video, clips do discovery
  (Acquired: 4h+ episodes, video mostly for Shorts/Reels; Lex Fridman: a
  separate clips channel, clips ~3-21 min).
- YouTube Podcasts: 1B+ monthly podcast viewers (Jan 2025); TV podcast
  viewing 400M+ h/month (2024); YouTube is the most-used US podcast platform
  (~31-33%, Edison). Marking a playlist as a podcast: counts for the US
  Weekly Top Podcast chart (May 2025, by watch time); clip-only playlists
  excluded; no evidence the label alone boosts recommendations. RSS
  ingestion is for audio-only shows: upload video directly and mark the
  playlist as a podcast. 2025-26 podcaster tools: AI clip suggestions,
  Shorts linking to the full episode. Spotify video podcasts (Partner
  Program Jan 2025) = an extra channel.
- Length: no study comparing full meeting vs trimmed vs topic-split.
  Suggested plan: full cleaned episode in a podcast playlist + 2-5 topic
  videos of 5-20 min + Shorts linking back.
- Premieres/live: no confirmed ranking benefit; streaming internal meetings
  live adds confidentiality risk.
- Audio: poor audio makes speakers seem less intelligent (2018 study, small
  samples). YouTube turns loud videos down but never quiet ones up:
  normalise to about -14 to -16 LUFS, -1 dBTP peak, level each speaker.
  Burned-in caption retention gains (15-28%) are vendor numbers.
- Shorts: view = every start/replay since 31 Mar 2025; compare our own
  Shorts of similar length rather than vendor benchmarks.

### R2-5. Traffic from outside YouTube (done, notes: offplatform_distribution.md)

- Google video search: since 4 Dec 2023 a page counts as a video result only
  if the video is the main content; a blog post with an embed ranks as text.
  Plan: a separate "watch page" per meeting (video on top, VideoObject
  markup, transcript below) and/or a blog article from the transcript, not
  one page doing both. Chapters in the YouTube description = Google "key
  moments" (cheapest win); Clip / SeekToAction markup on our own pages.
- AI search: YouTube is the #1 cited domain in Google AI Overviews (~21-25%
  across 2025-26 studies; Ahrefs 24.5% Nov 2026). 98.2% of YouTube
  citations come from Google's AI features; ChatGPT cites YouTube in ~2% of
  answers. Views/subscribers barely predict citation (41% of cited videos had
  under 1,000 views); Google often cites a timestamp (48%). So a small
  channel with clear titles, chapters and transcripts can be cited.
- LinkedIn: posts with external links get less reach (estimates 16-60%);
  native video ~3.5x the reach of a YouTube link; vertical video feed (2024).
  Posting through the official API is allowed; bots aren't.
- X: claims it removed its link penalty mid-2025; tests still show linked
  posts doing worse. Reddit: ~9 helpful posts per promotional one;
  r/MachineLearning uses a self-promotion thread. Hacker News: no promotion;
  "Show HN" excludes blog posts and videos.
- Paid: Studio "Promote" from $5 ($100+ recommended); its subs/watch hours
  don't count for YPP; reportedly no effect on organic recommendations.
  B2B cost per view ~$0.05-0.25 (vendor). Only for a few flagship videos.
- Measuring: UTM tags on links to flolabs.com; for traffic into YouTube use
  Studio's "External" source plus a tracked short link per channel.
- Automation: Google's "scaled content abuse" policy (Mar 2024) targets
  mass-produced pages. The notes have a table of what to generate per
  transcript (chapters, captions, watch page, blog/LinkedIn/X drafts, a
  clip, newsletter blurb, timestamped links for docs/READMEs, flags for
  Reddit/HN and paid promotion) and which need human edit/approval.

### R2-6. Channel features and engagement (done, notes: channel_features_engagement.md)

- Automatable via API: channel description, keywords, default language,
  country, trailer for non-subscribers (channels.update brandingSettings:
  read-modify-write, it replaces the whole object); banner; subscribe
  watermark (watermarks.set); home-page shelves (channelSections, may not
  work after redesigns); playlists + order; comment replies and new comments
  (force-ssl, 50 units each); live stream scheduling; captions;
  translated titles/descriptions (localizations).
- Studio/app only: handle, channel name, links, featured video for
  returning viewers, Posts/polls/quizzes, Communities, pinning/hearting
  comments, Collab invites, Premieres, end screens, cards, Shorts related
  link (unconfirmed), extra audio tracks, auto-dubbing, Hype.
- Eligibility: Posts: no subscriber minimum (advanced features on, not made
  for kids); Communities open to all with Posts (Jun 2025). Live: phone
  verification + ~24 h. Collaborations (1 Aug 2025): up to 5 (Help) or 10
  (vidIQ) creators, long videos/Shorts/archived lives. Hype: YPP channels
  with 500-500,000 subs, ~39 countries (26 Aug 2025), no effect on
  recommendations. Shopping: YPP + ~500 subs.
- Evidence of impact: only strong first-party number is multi-language
  audio (25%+ of watch time from other languages, Sep 2025). Playlists,
  comment replies, channel page lifting traffic: opinion only. Channel page =
  converting visitors, not a traffic source.
- Retired/flagged: brandingSettings comment moderation (7 Mar 2024);
  comments.markAsSpam, commentThreads.update, search.list relatedToVideoId
  deprecated; in-video promotion and "official series" playlists probably
  retired; a 3 Jun 2026 API deprecation notice needs checking.

### R2-7. Clipping tools and features to copy (done, notes: repurposing_tools.md)

- No independent test shows any clipping tool's features raise views.
  Every clipper ships the same core: transcript moment picking, a 0-100
  "virality score", 9:16 speaker tracking, word-by-word captions with
  emoji/keyword highlights, optional B-roll and scheduling.
- Virality scores weakly tied to results (reviewers; one unverified test:
  80+ scored clips got ~2.3x TikTok views of <50, with many exceptions).
  ACL 2025: an LLM predicted video popularity up to 85.5% accuracy; asking
  it to reason first helped. Do: LLM sub-scores (hook, stands alone, value),
  logged and checked against our own Analytics.
- Captions: only measured number is Facebook 2016 (+12% view time); 50% of
  US adults usually watch muted. Animated-caption "15-28%" claims are
  marketing. Build: WhisperX word timings -> ASS karaoke captions (Zoom VTT
  only times whole lines).
- Reframing: Zoom cloud recording can save active speaker and shared screen
  as separate files -> split layout (speaker top, screen bottom) with no
  face model (setting can fail or be admin-locked). Fallbacks: AutoFlip
  path smoothing, TalkNet/Light-ASD speaker detection, pyannote, MediaPipe,
  frame differencing to zoom into the active part of a screen share.
- Hooks/effects: first 1-3 s matter (viewed vs swiped); no evidence B-roll,
  sound effects, progress bars, AI voice or emojis raise views.
- Distribution: Test & Compare doesn't work on Shorts. TikTok posting API is
  private-only until reviewed. LinkedIn Videos API documented.
- Buy vs build: Vizard REST API + webhooks from ~$14.50/mo (yearly);
  Descript public API (Apr 2026); Submagic ~$69/mo for 100 API minutes;
  OpusClip API tier unclear. Open-source projects are hobby-grade.
- Suggested build order: (1) better LLM clip selection, strongest line
  first, stands-alone check, logged sub-scores; (2) trim silences/fillers
  by word gaps; (3) word-by-word ASS captions; (4) split layout from Zoom's
  separate files, blurred layout as fallback; (5) zoom into the active part
  of the screen share; (6) hook text in the first 2-3 s; (7) scheduled
  uploads (publishAt) + LinkedIn cross-posting; (8) skip B-roll/SFX/AI
  voice for now.
- Head-to-head test idea: same 5 meetings clipped by Vizard/OpusClip and by
  us; compare viewed-vs-swiped and average % viewed.

### R2-2. Keyword and topic research (done, notes: keyword_topic_research.md)

- Autocomplete: suggestqueries.google.com/complete/search?client=youtube
  &ds=yt&q=... is undocumented (no rate limit or terms; heavy use can get an
  IP blocked; can break). Blocked in this sandbox, so untested. Alphabet-soup
  and question/"vs"/"alternative" modifiers fully automatable. Paid
  fallbacks: Keyword Tool API (~$299/mo), Apify, Oxylabs.
- Volume tools all estimate and disagree (vidIQ, TubeBuddy, Keyword Tool,
  Ahrefs). Google Trends' YouTube Search filter = only Google-sourced YouTube
  demand, relative only. pytrends archived 17 Apr 2025; official Trends API
  alpha (Jul 2025) for approved testers only.
- Studio Research tab: top related searches (28 days), "across YouTube" and
  "your viewers", content-gap labels, English-only (US/UK/IN/AU/CA); no API,
  so a manual input. Inspiration tab -> Ask Studio (Aug 2026), no API.
- Competitor/outlier analysis: search.list 100 units, videos.list /
  channels.list 1 unit per 50 IDs (~100 searches/day on default quota).
  Outlier score = views / channel's typical views (average, or median of the
  previous 9-20 uploads); 2x = outlier, 10x = big. Stored stats must be
  refreshed/deleted within 30 days.
- Google video results: mostly informational, how-to, review queries; ~94%
  of carousel videos are YouTube; carousel on under 20% of queries (Semrush
  Aug 2026, down from 30%+). Intents for engineering content: how-to,
  explanation, "X vs Y", demo, failure/postmortem, trending.
- Transcript -> topics -> clips: no existing tool joins meeting topics to
  search demand; must build. Proposed 7 steps: segment, extract topics
  (LLM/KeyBERT), expand with autocomplete, score demand x gap x fit x clip
  quality, pick clips, titles containing the exact search phrase, screen for
  confidential content.
- Hashtags: 3 shown with the title, >60 = all ignored, guides say 3-5; no
  hashtag-volume source.

### R2-8. Analytics and experiments (done, notes: analytics_experiments.md)

- CTR falls as a video reaches beyond its core audience (official, "a sign
  of success"); search CTR > browse CTR. Third-party benchmark 4-6%; trust a
  CTR only after ~1-2k impressions.
- View counting: Shorts views = every play since 31 Mar 2025 (old count =
  "engaged views"); long-form reportedly split the same way on 24 Aug 2026.
  Store engagedViews alongside views; time series break at those dates.
- Impressions/CTR: Reporting API reach reports (added 15 Jan 2026):
  channel_reach_basic_a1 with video_thumbnail_impressions and
  video_thumbnail_impressions_ctr (+ a variant with traffic source/device);
  not in Analytics reports.query; first report up to 48 h after creating the
  job; Pacific-time days.
- Analytics API: retention = elapsedVideoTimeRatio (100 points) with
  audienceWatchRatio and relativeRetentionPerformance (<0.5 = worse than
  similar-length videos); search terms = insightTrafficSourceDetail filtered
  to YT_SEARCH, maxResults <= 25; 48-72 h lag (use videos.list for live
  counts); traffic-source queries fail if videos x days > 50,000; new API
  terms on derived metrics/storage from 1 Jun 2026.
- Benchmarks (third-party): Vidyard 2025 (~1M B2B videos) ~65% finish
  videos under 1 min, ~20% finish videos over 20 min; Wistia ~50% (<1 min),
  38% (5-30 min); 8-12 min education/B2B 42-55% average viewed. vidIQ
  (9,414 channels): median ~16 months to 1,000 subscribers.
- Diagnosis (Paddy Galloway): compare each video with the channel's own
  typical; low CTR = packaging problem, steep early drop = intro doesn't
  match the title; find retention "danger zones"; median baselines; don't
  change packaging early if CTR and AVD look fine.
- Test & Compare: winner by watch-time share; results Winner / Performed
  Same / Inconclusive (inconclusive keeps variant 1); titles global in 2026,
  up to 3, up to 2 weeks; no published statistics method.
- Studio-only (likely): Test & Compare results, typical-performance rank,
  Research tab, Ask Studio, Audience tab reports, viewed vs swiped away,
  new/returning and unique viewers.
- No documented production system tunes LLM titles/clips from analytics.
  Nearest: Thompson sampling with LLM-generated treatments (arXiv
  2505.16311), LLM priors updated by clicks (arXiv 2608.03382), katcha
  (open source).
- Suggested app design: daily Reporting API ingest + per-video reports.query
  from day 3; median/quartile baselines per format at day 2/7/28; weekly
  digest and alerts (day-7 impressions in bottom quarter, low relative
  retention in the first 10%, any video at 2x median or more); pool
  experiments across videos by packaging pattern (Bayesian / Thompson
  sampling) with watch time per impression as reward; pick Shorts from peaks
  in audienceWatchRatio; human approval; log every packaging change.

### R2-1. How the recommendation system works (done, notes: algorithm_deep_dive.md)

- Papers: 2016 two-stage system (candidates, then ranking) on expected
  watch time, with a freshness feature; 2019 "watch next" ranker balances
  engagement and satisfaction, corrects position bias; 2021 (official)
  signals = clicks, watch time, surveys (predicted for non-answerers),
  shares, likes/dislikes. KDD 2023: a separate fresh-content stack gives
  extra exposure to videos with little history (+0.45% dwell time for small
  providers). 2025-26 (PLUM, Token Factory): Gemini-based retrieval picks
  videos by IDs built from content + metadata: a clear topic in title,
  description, transcript and chapters is how a brand-new video gets picked
  up at all.
- Suggested: based on what viewers of a video also watch and enjoy (co-watch),
  not topic match or look-alike thumbnails. Strategy: be the natural "next
  watch" for a specific bigger video's audience, then build our own co-watch
  with playlists, end screens and Shorts.
- New channels: no official "new channel boost"; each video judged on its
  own; new videos reach non-viewers within the first hour; YouTube has teams
  helping small creators (VidCon 2025). A hard topic pivot slows traction.
- Notifications: max 3 new-video notifications per channel per 24 h; more
  than 3 in a short window can pause them for 24 h -> stagger publishes
  (today we post 4 shorts + the full video at once!). Push alerts stop for
  inactive "All"-bell subscribers (~Apr 2026). Subscribers skip ~90% of
  uploads; Subscriptions-feed CTR under 10% is normal.
- External traffic doesn't hurt recommendations (each surface learns from its
  own traffic); benefit is indirect. View/sub exchanges break policy.
- Trending page removed ~21-22 Jul 2025 (category Charts instead); Explore
  and Hype leaderboard remain. New: Ask YouTube AI search (May 2026, 140M+
  users by June), Custom Feeds from viewer prompts (23 Sep 2026), Shorts
  originality update (1 Oct 2026, helps us: our Shorts are original).
- Myths officially denied: upload-time delays, "first hours decide all",
  channel-average penalties, external traffic hurting, Shorts hurting
  long-form in general, Hype boosting ranking, tags mattering much.
  Prohibited: sub4sub, bought engagement. Re-uploading the same Short may be
  treated as spam.

Status: all 8 round-2 strands done; combined report next.

## Round 2 report (done)

`reports/YouTube traffic methods in depth.md` (~13,800 words, 32 methods
ranked, API matrix, 4-phase roadmap, per-method build plans, quick wins,
benchmarks, risks, 19 facts to verify). Builds on and corrects round 1.

Top methods: (1) grounded packaging pass inside the existing videos.insert
call; (2) staggered publishing (max 3 bell notifications per channel per
24 h; we post 4 Shorts + the full video at once today); (3) review window
via publishAt + decide/screen.py confidentiality hold; (4) Shorts that open
on the claim, hook of 6 words or fewer, zoom-in HD encode.

Corrections vs round 1: impressions/CTR come from the Reporting API reach
reports; Test & Compare excludes Shorts; topic cuts (single-topic 5-20 min
videos linked to the full meeting) replace "post the highlights reel" (a
hypothesis to test). New: loudness normalisation missing in the pipeline;
highlights reel should open on what the title promises; Zoom word timings
are interpolated, so word-by-word captions need a per-clip aligner.

Roadmap:
- Phase 0 (this week, no code): Studio channel setup, phone verification,
  podcast/series decisions, Zoom HD + separate-file recording, quota/audit
  check, hero-topic choice, interim private-by-default autopilot.
- Phase 1 (weeks 1-4, no new scope): packaging pass with promise check and
  3 packages; metadata on insert; staggered schedule + review window;
  confidentiality screen; Shorts hook overlay, claim-first window, zoom-in HD;
  real-frame thumbnails; loudness normalisation; Studio checklist in panel.
- Phase 2 (weeks 4-10, reconnect with youtube.force-ssl +
  yt-analytics.readonly): captions; series/podcast playlists; self-healing
  descriptions; topic cuts; demand research; analytics + weekly digest;
  localizations; discussion comment; word-level captions where possible.
- Phase 3 (months 3-6): companion pages with JSON-LD; LinkedIn/newsletter
  kit; learning loop into packaging; split layout + screen zoom; competitor
  outliers; Reddit/HN flags.
- Phase 4 (optional): paid promotion of flagships; auto-dubbing test; vendor
  clipper bake-off; Shorts video-cut tests.

Test before building: whether publishAt works for Shorts and whether
notifySubscribers exists (one private test Short); upload quota in the Cloud
console; private-lock rule for unaudited projects.

## Next steps (after round 2)

1. Owner picks what to build first (recommended: Phase 0 checklist now,
   then Phase 1 items 1-4).
2. Verify the flagged facts on the live official pages.
3. Build, test and deploy one change at a time.
