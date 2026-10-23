# Highlight Cutter: ازاي اتبنى وازاي اترفع على AWS

الدليل ده بيشرح المشروع من الأول للآخر: بيعمل إيه، ومبني ازاي، وازاي رفعناه على AWS خطوة بخطوة، وازاي ربطناه بيوتيوب وZoom. اتكتب من تجربة الرفع الحقيقية على `cutter.flolabsinnovations.com`، ونفس الخطوات تنفع لأي باك إند Python تاني.

> **قاعدة ثابتة:** المفاتيح (API keys، باسوردات، secrets) عمرها ما تتكتب في الكود ولا في الدليل ده ولا تترفع على GitHub. مكانها ملف `.env` على السيرفر وعلى جهازك بس، والملف ده موجود في `.gitignore`.

## المحتويات

- [الجزء الأول: المشروع نفسه](#الجزء-الأول-المشروع-نفسه)
  - [1. الأداة بتعمل إيه](#1-الأداة-بتعمل-إيه)
  - [2. مسار الميتنج جوه الأداة](#2-مسار-الميتنج-جوه-الأداة)
  - [3. الفولدرات والملفات](#3-الفولدرات-والملفات)
  - [4. التقنيات المستخدمة وليه](#4-التقنيات-المستخدمة-وليه)
  - [5. الـ API](#5-الـ-api)
  - [6. تشغيله على جهازك](#6-تشغيله-على-جهازك)
- [الجزء التاني: الرفع على AWS](#الجزء-التاني-الرفع-على-aws)
  - [7. الصورة الكبيرة](#7-الصورة-الكبيرة)
  - [8. قبل ما تبدأ](#8-قبل-ما-تبدأ)
  - [9. تجهيز المشروع للرفع](#9-تجهيز-المشروع-للرفع)
  - [10. عمل السيرفر على AWS](#10-عمل-السيرفر-على-aws)
  - [11. IP ثابت والدخول على السيرفر](#11-ip-ثابت-والدخول-على-السيرفر)
  - [12. تجهيز السيرفر](#12-تجهيز-السيرفر)
  - [13. المفاتيح والبناء والتشغيل](#13-المفاتيح-والبناء-والتشغيل)
  - [14. الدومين و HTTPS](#14-الدومين-و-https)
  - [15. ربط يوتيوب (OAuth)](#15-ربط-يوتيوب-oauth)
  - [16. ربط Zoom](#16-ربط-zoom)
- [الجزء التالت: التشغيل اليومي](#الجزء-التالت-التشغيل-اليومي)
  - [17. الشغل اليومي](#17-الشغل-اليومي)
  - [18. تعديل الكود ونشره](#18-تعديل-الكود-ونشره)
  - [19. مشاكل قابلتنا وحلها](#19-مشاكل-قابلتنا-وحلها)
  - [20. أمان وتكلفة](#20-أمان-وتكلفة)

---

# الجزء الأول: المشروع نفسه

## 1. الأداة بتعمل إيه

بتاخد تسجيل ميتنج، وبتطلع منه نسخة مقصوصة، وبعدين تنشرها على قناة يوتيوب الشركة وتمسحها من السيرفر.

فيه نوعين قص:

| النوع | بيعمل إيه |
|---|---|
| **Highlights** | بيسيب أهم الأجزاء بس (القرارات، التحديثات، النقاط المهمة) |
| **Light cleanup** | بيسيب الميتنج كله، وبيشيل الكلام اللي فوق بعض (crosstalk) |

والميتنج بيدخل الأداة بتلات طرق:

| الطريقة | إمتى |
|---|---|
| **From Zoom** (الافتراضي) | بتختار الميتنج من لستة تسجيلات Zoom Cloud بتاعة الشركة، والأداة بتنزّل الفيديو والترانسكريبت لوحدها |
| **Paste a link** | لينك يوتيوب. على السيرفر غالباً مش هيشتغل لأن يوتيوب بيعمل block لـ IPs الداتا سنترز |
| **Drop a file** | ترفع ملف فيديو، ومعاه ملف ترانسكريبت `.vtt` أو `.srt` لو موجود |

## 2. مسار الميتنج جوه الأداة

```
Zoom / ملف / لينك
   │
   ▼
[ingest]      تنزيل أو حفظ الفيديو + فحص إن الصوت والصورة متزامنين (ffprobe)
   │
   ▼
[transcribe]  لو فيه ترانسكريبت جاهز (Zoom أو مرفوع أو captions يوتيوب) بيستخدمه
              لو مفيش: WhisperX يفرّغ الكلام ويعرف مين اللي بيتكلم (تقيل ومحتاج HF_TOKEN)
   │
   ▼
[overlap]     يعلّم الأماكن اللي فيها كلام فوق بعض
   │
   ▼
[decide]      Claude (أو Gemini) يقرا الترانسكريبت على دفعات ويقرر لكل جزء: keep أو remove
   │
   ▼
[edl]         يحوّل القرارات لقايمة مقاطع نظيفة (Edit Decision List): يرتّب، يدمج، ويقص على حدود الكلام
   │
   ▼
[slice]       ffmpeg يقص المقاطع ويلزقها في فيديو واحد + ترانسكريبت متظبط على الفيديو الجديد
   │
   ▼
[publish]     رفع على يوتيوب (Private أو Unlisted أو Public) + مسح الملفات من السيرفر
```

كل ميتنج بيتعامل كـ **job** ليها رقم، وحالتها بتتحفظ في ملف `job.json`. الواجهة بتسأل السيرفر عن الحالة كل 3 ثواني وبتعرض التقدم.

## 3. الفولدرات والملفات

| المكان | فيه إيه |
|---|---|
| `api/main.py` | كل الـ endpoints (FastAPI) |
| `api/jobs.py` | تخزين حالة الـ jobs في ملفات JSON، وتعليم الـ jobs اللي اتقطعت بسبب restart إنها failed |
| `core/config.py` | كل الإعدادات، بتتقري من `.env` أو environment variables |
| `core/models.py` | شكل البيانات (JobRecord، Segment، Decision، EDL) |
| `ingest/` | `store.py` حفظ الرفع، `validate.py` فحص الفيديو، `youtube.py` تنزيل لينك، `zoom.py` استيراد من Zoom |
| `transcribe/` | `native.py` قراية VTT/SRT و captions يوتيوب، `whisperx_backend.py` التفريغ الذاتي، `overlap.py` الكلام المتداخل |
| `decide/` | `claude_client.py` و `gemini_client.py` قرارات الـ keep/remove، و `__init__.py` بيختار حسب `LLM_PROVIDER` |
| `edl/builder.py` | بناء قايمة المقاطع النهائية |
| `slice/` | أوامر ffmpeg، وإعادة ضبط الترانسكريبت على الفيديو المقصوص |
| `publish/` | `youtube.py` الربط بالقناة والرفع، `cleanup.py` مسح الملفات |
| `pipeline.py` | بيربط كل المراحل ببعض |
| `web/index.html` | الواجهة كلها (صفحة واحدة) |
| `web/privacy.html` | سياسة الخصوصية اللي جوجل طالبها، مفتوحة من غير باسورد |
| `deploy/` | ملفات السيرفر: `Caddyfile`، `setup-ec2.sh`، `cleanup.sh`، `zoom-check.py` |
| `Dockerfile` و `docker-compose.yml` | تشغيل المشروع في containers |
| `tests/` | الاختبارات (pytest) |
| `.github/workflows/tests.yml` | GitHub بيشغّل ruff و pytest و shellcheck على كل push |

## 4. التقنيات المستخدمة وليه

| التقنية | ليه |
|---|---|
| **Python 3.12 + FastAPI** | API بسيط وسريع، وبيشغّل الـ jobs في الخلفية (BackgroundTasks) من غير Redis ولا Celery |
| **ffmpeg** | القص واللزق. بيشتغل في الـ container نفسه |
| **Claude (Anthropic API)** | قرارات القص. بنستخدم structured outputs عشان الرد يرجع JSON مضبوط دايماً. Gemini موجود كبديل بتغيير `LLM_PROVIDER` |
| **WhisperX** | تفريغ ذاتي لو مفيش ترانسكريبت. تقيل على الـ CPU، فبنحاول دايماً نستخدم ترانسكريبت Zoom بدله |
| **Zoom API (Server-to-Server OAuth)** | لستة التسجيلات وتنزيل الفيديو والترانسكريبت |
| **YouTube Data API v3 (OAuth)** | الرفع على قناة الشركة |
| **Docker + Docker Compose** | نفس البيئة على أي جهاز، وتشغيل بأمر واحد |
| **Caddy** | HTTPS أوتوماتيك من Let's Encrypt + باسورد على الموقع كله |
| **AWS EC2** | سيرفر واحد بسيط تفهمه وتصلحه |

ليه مفيش داتابيز؟ لأن الحالة صغيرة وبتتحفظ في ملفات JSON، والفيديوهات بتتمسح بعد النشر. لو المشروع كبر واحتاج داتابيز، يتضاف RDS في نفس الـ VPC.

## 5. الـ API

| الطلب | بيعمل إيه |
|---|---|
| `GET /zoom/recordings` | لستة تسجيلات Zoom آخر 30 يوم، وكل واحد جاهز ولا لأ |
| `POST /jobs/zoom` | `{"meeting_uuid", "mode", "title"}`: يبدأ job من ميتنج Zoom |
| `POST /jobs` | رفع ملف (`file`، و `transcript` اختياري، و `mode`) |
| `POST /jobs/youtube` | `{"url", "mode"}` |
| `GET /jobs/{id}` | الحالة والتقدم |
| `GET /jobs/{id}/video` و `/edl` و `/transcript` | النتايج |
| `POST /jobs/{id}/publish` | `{"title", "description", "privacy", "delete_after"}` |
| `DELETE /jobs/{id}/files` | مسح الفيديوهات والاحتفاظ بالسجل |
| `GET /youtube/status`، `/youtube/connect`، `POST /youtube/disconnect` | ربط قناة يوتيوب |
| `GET /healthz` | بترد `ok`. Docker بيستخدمها يعرف إن الـ app شغال |
| `GET /privacy` | سياسة الخصوصية |

كل ده ورا باسورد Caddy ما عدا `/privacy`. لو هتنادي الـ API من n8n أو سكريبت، ابعت نفس اليوزر والباسورد كـ `Authorization: Basic ...`.

## 6. تشغيله على جهازك

محتاج Python من 3.10 لـ 3.12 و ffmpeg.

```powershell
cd "D:\the work\flolabs\international\meetings_automat"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn api.main:app --reload
```

افتح `http://localhost:8000`. الإعدادات بتتقري من `.env` اللي في نفس الفولدر. ملحوظة: ربط يوتيوب على جهازك مش هيشتغل غير لو ضفت `http://localhost:8000/youtube/callback` في Google Cloud، وده مش ضروري لأن النشر بيتعمل من السيرفر.

الاختبارات:

```powershell
pytest -q
ruff check .
```

---

# الجزء التاني: الرفع على AWS

## 7. الصورة الكبيرة

```
المتصفح ──HTTPS──► Caddy (بورت 443، باسورد) ──► app (FastAPI، بورت 8000 جوه Docker)
                                                  │
                                                  ├──► Zoom API     (تنزيل الميتنج)
                                                  ├──► Claude API   (قرارات القص)
                                                  └──► YouTube API  (النشر)
```

سيرفر EC2 واحد عليه Docker. فيه containerين: `app` (المشروع) و `caddy` (HTTPS والباسورد). الـ app مش مفتوح على الإنترنت مباشرة، Caddy بس اللي بيوصله. الفيديوهات بتتحفظ مؤقتاً في `data/` وبتتمسح بعد النشر، وفيه cron بيمسح أي حاجة عدّى عليها 3 أيام.

## 8. قبل ما تبدأ

| المطلوب | ليه | عندنا |
|---|---|---|
| حساب AWS بصلاحية EC2 | عشان تعمل السيرفر | حساب flolabs، ريجون Frankfurt |
| الكود على GitHub | السيرفر بيسحب الكود منه | `FlolabsInnovationsGroup/ahsan_meetings_automation` (private) |
| دومين تقدر تعدّل الـ DNS بتاعه | HTTPS وربط جوجل | `flolabsinnovations.com` على Dynadot |
| مفاتيح الـ APIs | الكود بيقراها من `.env` | Anthropic، Zoom، YouTube |
| جهاز عليه SSH | عشان تدخل السيرفر | PowerShell على Windows |

## 9. تجهيز المشروع للرفع

الملفات دي اتضافت للـ repo مرة واحدة، وبتخلي السيرفر يشغّل المشروع بأمر واحد:

| الملف | بيعمل إيه |
|---|---|
| `Dockerfile` | بيبني image فيها Python و ffmpeg و torch نسخة CPU وكل المكتبات. بيشتغل بيوزر `app` مش root |
| `.dockerignore` | بيمنع `.env` و `.git` والفيديوهات إنها تدخل الـ image |
| `docker-compose.yml` | بيشغّل `app` و `caddy` مع بعض، وبيحدد مكان الداتا، وبيحدد حجم اللوجز |
| `deploy/Caddyfile` | HTTPS أوتوماتيك، باسورد على كل حاجة ما عدا `/privacy`، ورفع لحد 4 جيجا |
| `deploy/setup-ec2.sh` | بيسطّب Docker، و swap لو الرامات أقل من 7 جيجا، والمسح الأوتوماتيك، والتحديثات الأمنية |
| `deploy/cleanup.sh` | بيمسح الملفات القديمة كل يوم عشان الديسك ما يتملاش |
| `.env.example` | أسماء المتغيرات من غير قيمها |
| `.gitignore` | فيه `.env` و `deploy/caddy.env` و `data/` |
| `.gitattributes` | بيخلي ملفات `.sh` تتحفظ بـ LF. من غيره السكريبتات اللي اتكتبت على Windows بتفشل على Linux |

وفي الكود نفسه اتعملت تلات حاجات مهمة للـ production:

- **`/healthz`**: الـ healthcheck بتاع Docker بيستخدمها، و Caddy مش بيشتغل غير لما الـ app يبقى healthy.
- **كل الإعدادات من environment variables**: مفيش مسارات Windows ولا مفاتيح مكتوبة في الكود.
- **worker واحد (`--workers 1`)**: لأن الـ jobs بتشتغل جوه نفس الـ process. ولو السيرفر عمل restart وفيه job شغالة، الـ app بيعلّمها failed أول ما يقوم بدل ما تفضل متعلقة.
- **نسخ المكتبات متثبتة**: WhisperX متثبت على commit محدد عشان ما يتغيرش فجأة ويكسر البناء.

## 10. عمل السيرفر على AWS

### أ) Budget alarm الأول

Billing and Cost Management > Budgets > Create budget. اختار Monthly cost budget، وحط مبلغ (مثلاً 100$) وإيميلك. كده هتجيلك رسالة قبل ما الفاتورة تفاجئك.

### ب) الريجون

من فوق على اليمين اختار **Europe (Frankfurt) eu-central-1**، لأنه قريب من مصر وتونس وتركيا واليونان. كل الخطوات الجاية في نفس الريجون.

### ج) EC2 > Launch instance

| الخانة | القيمة | ملاحظة |
|---|---|---|
| Name | `highlight-cutter` | أي اسم واضح |
| AMI | Ubuntu Server 24.04 LTS، x86 | مش 26.04 (جديدة وممكن Docker ما يدعمهاش)، ومش Ubuntu Pro (بفلوس زيادة) |
| Instance type | `t3.large` (2 vCPU، 8 GiB) | `t3.micro` مش هتكفي حتى البناء |
| Key pair | Create new، ED25519، `.pem` | احفظ الملف في مكان ثابت. لو ضاع مش هتعرف تدخل السيرفر |
| SSH | Source: **My IP** | عمره ما يبقى Anywhere |
| HTTP و HTTPS | ✅ الاتنين | HTTP لازم عشان شهادة Let's Encrypt |
| Storage | 30 GiB، gp3 | تقدر تكبّره بعدين، بس ما تقدرش تصغّره |
| Encrypted | Encrypted، KMS key: `(default) aws/ebs` | من السهم اللي جنب Volume 1 |

ليه 30 جيجا مش أكتر؟ لأن الفيديوهات مش بتفضل على السيرفر: بتتنشر وتتمسح. أغلب المساحة بتروح لـ Docker image (فيها torch).

لما تغيّر الـ AMI، AWS بيسألك إن إعدادات هتتغير. اضغط Confirm، وراجع الـ security group والديسك تاني. التحذير الأصفر "Rules with source of 0.0.0.0/0" سببه HTTP و HTTPS، وده مقصود.

عندنا: السيرفر `i-0f8721a2ee5b56565` في `eu-central-1`.

## 11. IP ثابت والدخول على السيرفر

### أ) Elastic IP

من غيره، الـ IP بيتغير كل ما السيرفر يقف ويشتغل، والدومين هيبطل يشاور عليه.

1. EC2 > Network & Security > **Elastic IPs**.
2. **Allocate Elastic IP address** > Allocate.
3. **Associate this Elastic IP address**: Resource type = Instance، اختار السيرفر، > Associate.

عندنا الـ IP هو `3.126.252.9`.

### ب) SSH من Windows

استخدم "Windows PowerShell" العادي، مش نسخة (x86) لأنها مش بتشوف أمر `ssh`. لو مفيش غيرها، استخدم `C:\Windows\Sysnative\OpenSSH\ssh.exe`.

أول مرة بس، ظبط صلاحيات ملف الـ `.pem`، وإلا `ssh` هيرفضه برسالة `UNPROTECTED PRIVATE KEY FILE`:

```powershell
$k = "C:\Users\<اسمك>\Downloads\highlight-cutter.pem"
icacls $k /inheritance:r
icacls $k /grant:r "$($env:USERNAME):(R)"
icacls $k
```

آخر أمر لازم يطبع يوزر واحد بس. لو فيه `UNKNOWN`، امسحه بالـ SID اللي ظاهر: `icacls $k /remove "*S-1-5-21-..."`.

بعدها ادخل:

```powershell
ssh -o ServerAliveInterval=30 -i $k ubuntu@3.126.252.9
```

أول مرة هيسأل yes/no، اكتب `yes`. لما يظهر `ubuntu@ip-...:~$` تبقى جوه السيرفر.

## 12. تجهيز السيرفر

### أ) Deploy key (السيرفر يقرا الـ repo)

الـ repo private، فالسيرفر محتاج مفتاح خاص بيه، بيقرا الـ repo ده بس ومالوش صلاحية كتابة:

```bash
ssh-keygen -t ed25519 -C "highlight-cutter-ec2" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

انسخ السطر اللي اتطبع. في GitHub: الـ repo > Settings > **Deploy keys** > Add deploy key. الصقه، وسيب Allow write access **من غير** علامة.

### ب) Clone وسكريبت الإعداد

```bash
ssh -o StrictHostKeyChecking=accept-new -T git@github.com
git clone git@github.com:FlolabsInnovationsGroup/ahsan_meetings_automation.git
cd ahsan_meetings_automation
sudo bash deploy/setup-ec2.sh
exit
```

أول أمر لازم يرد بـ `successfully authenticated`. الـ `exit` مهم: اخرج وادخل تاني عشان تستخدم `docker` من غير `sudo`. بعدها اتأكد:

```bash
cd ahsan_meetings_automation && docker ps && free -h && df -h /
```

## 13. المفاتيح والبناء والتشغيل

### أ) ملف `.env` على السيرفر

```bash
cp .env.example .env && chmod 600 .env
```

عشان تحط مفتاح من غير ما يظهر على الشاشة أو يتحفظ في الـ history، استخدم `read -s` (الصق بكليك يمين واضغط Enter):

```bash
read -s -p "Anthropic key: " K && sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$K|" .env && unset K && echo && grep -c "^ANTHROPIC_API_KEY=sk-ant" .env
```

لو طبع `1` يبقى اتحفظ. نفس الفكرة لأي مفتاح تاني (غيّر اسم المتغير). وغيّر `LLM_PROVIDER=anthropic`.

المتغيرات اللي السيرفر بيستخدمها:

| المتغير | لازم؟ | ملاحظة |
|---|---|---|
| `LLM_PROVIDER` | ✅ | `anthropic` |
| `ANTHROPIC_API_KEY` و `ANTHROPIC_MODEL` | ✅ | |
| `ZOOM_ACCOUNT_ID` و `ZOOM_CLIENT_ID` و `ZOOM_CLIENT_SECRET` | ✅ | تاب From Zoom |
| `ZOOM_USER` | اختياري | `me` (صاحب الحساب) أو إيميل يوزر تاني |
| `YOUTUBE_CLIENT_ID` و `YOUTUBE_CLIENT_SECRET` | ✅ | خطوة 15 |
| `PUBLIC_BASE_URL` | ✅ | `https://cutter.flolabsinnovations.com` |
| `HF_TOKEN` | اختياري | بس لو هتستخدم WhisperX (ميتنج من غير ترانسكريبت) |
| `GEMINI_API_KEY` | اختياري | بس لو `LLM_PROVIDER=gemini` |

### ب) باسورد الموقع (`deploy/caddy.env`)

```bash
cp deploy/caddy.env.example deploy/caddy.env && chmod 600 deploy/caddy.env
docker run --rm caddy:2 caddy hash-password --plaintext '<باسورد قوي>'
nano deploy/caddy.env
```

جوه الملف: `DOMAIN` هو الدومين، و `BASIC_AUTH_USER` اسم الدخول، و `BASIC_AUTH_HASH` الـ hash اللي اتطبع بين علامات تنصيص فردية `'...'` لأن فيه علامات `$`. قبل ما يبقى عندك دومين، تقدر تستخدم `3-126-252-9.sslip.io`.

### ج) البناء والتشغيل

البناء أول مرة بياخد من 10 لـ 20 دقيقة. شغّله بـ `nohup` عشان لو الاتصال اتقطع ما يقفش:

```bash
nohup docker compose up -d --build > build.log 2>&1 &
until docker compose ps app 2>/dev/null | grep -q healthy; do sleep 30; tail -1 build.log; done; docker compose ps
```

لما `app` يبقى healthy و `caddy` يبقى Up، اتأكد من شهادة HTTPS:

```bash
docker compose logs caddy | grep -i "certificate obtained" | tail -2
```

## 14. الدومين و HTTPS

### أ) Dynadot

1. My Domains > Manage Domains > اختار `flolabsinnovations.com`.
2. DNS Settings: اتأكد إن مكتوب Dynadot DNS.
3. ⚠️ ما تلمسش قسم "1. Domain Record" (A و TXT و MX)، دول بتوع الموقع والإيميل.
4. في "2. Subdomain Records" > + Add Record:

| Subdomain | Record Type | IP Address |
|---|---|---|
| `cutter` | A | `3.126.252.9` |

5. Save Settings.

### ب) اتأكد وشغّله

من PowerShell على جهازك (لازم يطلع `Address: 3.126.252.9`):

```powershell
nslookup cutter.flolabsinnovations.com
```

على السيرفر:

```bash
sed -i 's/^DOMAIN=.*/DOMAIN=cutter.flolabsinnovations.com/' deploy/caddy.env
sed -i 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://cutter.flolabsinnovations.com|' .env
docker compose up -d --force-recreate caddy app
sleep 20; docker compose logs caddy | grep -i "certificate obtained" | tail -2
```

استخدم `--force-recreate` دايماً لما تغيّر ملف إعدادات، لأن Docker مش بيشوف التغيير لوحده.

## 15. ربط يوتيوب (OAuth)

السيرفر محتاج إذن يرفع على قناة الشركة. جوجل بيدي الإذن ده عن طريق "تطبيق" في Google Cloud: صاحب القناة بيسجل دخول مرة واحدة، والسيرفر بيحتفظ بتصريح دائم (refresh token) في `data/youtube_token.json`. الخدمة مجانية ومش محتاجة كارت، وفي حدود 100 رفع في اليوم (الرفع ليه quota منفصلة عن باقي الـ API).

كل الخطوات بحساب جوجل اللي ماسك القناة، على `console.cloud.google.com`:

1. **Project جديد**: مثلاً `flolabs-highlight-cutter`. لو طلب Free trial أو كارت، تجاهله.
2. **فعّل الـ API**: APIs & Services > Library > **YouTube Data API v3** > Enable.
3. **OAuth consent screen** > Audience: **External** (لأن الحساب Gmail عادي).
4. **Branding**:
   - App home page: `https://flolabsinnovations.com`
   - Privacy policy: `https://cutter.flolabsinnovations.com/privacy`
   - Authorized domains: `flolabsinnovations.com`
   - ⚠️ **ما ترفعش لوجو**. اللوجو بيخلي جوجل يطلب verification، وده بياخد أسابيع.
5. **Audience** > Publish app > Confirm، لحد ما يبقى **In production**. لو فضل Testing، التصريح بيتلغي كل 7 أيام.
6. **Clients** > Create client > Web application. في Authorized redirect URIs: `https://cutter.flolabsinnovations.com/youtube/callback`. انسخ الـ Client ID والـ secret قبل ما تقفل النافذة.

على السيرفر:

```bash
read -s -p "Client ID: " CID && echo && read -s -p "Client secret: " CSEC && echo
sed -i "s|^YOUTUBE_CLIENT_ID=.*|YOUTUBE_CLIENT_ID=$CID|; s|^YOUTUBE_CLIENT_SECRET=.*|YOUTUBE_CLIENT_SECRET=$CSEC|" .env && unset CID CSEC
docker compose up -d --force-recreate app
```

الربط: افتح `https://cutter.flolabsinnovations.com/youtube/connect` بحساب القناة. هتظهر "Google hasn't verified this app": اضغط Advanced > Go to ... (unsafe)، وعلّم على كل الصلاحيات. بعدها `/youtube/status` لازم يرد `"connected":true` واسم القناة.

## 16. ربط Zoom

بدل ما تنزّل الميتنج من Zoom وترفعه، الأداة بتشوف تسجيلات Zoom Cloud بتاعة الشركة مباشرة.

1. `marketplace.zoom.us` > Develop > Build App > **Server-to-Server OAuth**، وفعّله (Activate).
2. Scopes:
   - `cloud_recording:read:list_user_recordings:admin` (اللستة)
   - `cloud_recording:read:recording:admin` (ملفات الميتنج الواحد)
   - اختياري: `cloud_recording:read:list_account_recordings:admin` لو عاوز ميتينجز كل اليوزرز في الحساب مش صاحب الحساب بس (محتاج تعديل بسيط في الكود).
3. حط `ZOOM_ACCOUNT_ID` و `ZOOM_CLIENT_ID` و `ZOOM_CLIENT_SECRET` في `.env` بـ `read -s` زي خطوة 13.
4. اتأكد من فولدر المشروع على السيرفر:

```bash
python3 deploy/zoom-check.py
```

لازم يطبع `TOKEN OK` والـ scopes، و`USER (me) recordings: HTTP 200` وعدد الميتينجز. بعدها `docker compose up -d --force-recreate app`.

5. في Zoom: Settings > Recording > Cloud recording، فعّل **Create audio transcript**. من غيره الميتينج مش هيبقى ليه ترانسكريبت، والأداة هتعرضه رمادي.

الأداة بتختار أحسن نسخة فيديو (shared screen with speaker view لو موجودة)، وبتستخدم ترانسكريبت Zoom (أو الـ Closed Captions لو مفيش غيرها)، فمفيش WhisperX ولا ضغط على الـ CPU. ومفيش حاجة بتتمسح من Zoom نفسه.

---

# الجزء التالت: التشغيل اليومي

## 17. الشغل اليومي

كل الأوامر دي جوه فولدر المشروع على السيرفر (`cd ~/ahsan_meetings_automation`):

| عاوز | الأمر |
|---|---|
| تنزّل نسخة جديدة من الكود | `git pull && docker compose up -d --build` |
| تشوف لوجز الـ app | `docker compose logs -f app` (و Ctrl+C للخروج) |
| تشوف حالة الخدمات | `docker compose ps` |
| restart للـ app | `docker compose restart app` |
| تطبّق تغيير في `.env` أو `caddy.env` | `docker compose up -d --force-recreate app` (أو `caddy`) |
| تشوف الديسك | `df -h / && du -sh data/*` |
| سجل المسح الأوتوماتيك | `cat /var/log/highlight-cutter-cleanup.log` |
| تختبر Zoom | `python3 deploy/zoom-check.py` |

الـ restart أو التحديث وفيه ميتنج بيتقص بيوقفه، والـ app بيعلّمه failed. اعمل التحديث في وقت مفيش حد بيستخدم الأداة.

## 18. تعديل الكود ونشره

التعديل بيتعمل على جهازك، مش على السيرفر. السيرفر بياخد الكود بـ `git pull` بس.

```powershell
cd "D:\the work\flolabs\international\meetings_automat"
git pull                      # هات آخر نسخة الأول
# ... عدّل الكود ...
pytest -q; ruff check .       # اتأكد إن كل حاجة شغالة
git add -A
git commit -m "وصف التعديل"
git push
```

GitHub بيشغّل الاختبارات لوحده على كل push (تاب Actions). بعدها على السيرفر:

```bash
cd ~/ahsan_meetings_automation && git pull && docker compose up -d --build
```

⚠️ ما تعملش `git push --force` على `main`. لو حد عمله، السيرفر هيقول `divergent branches` (شوف الجدول تحت).

## 19. مشاكل قابلتنا وحلها

| المشكلة | السبب | الحل |
|---|---|---|
| `ssh is not recognized` | PowerShell نسخة (x86) | Windows PowerShell العادي، أو `C:\Windows\Sysnative\OpenSSH\ssh.exe` |
| `UNPROTECTED PRIVATE KEY FILE` | يوزر تاني عنده صلاحية على الـ `.pem` | أوامر `icacls` في خطوة 11 |
| Connection reset بعد شوية | الاتصال قاعد من غير استخدام | ادخل بـ `-o ServerAliveInterval=30`. اللي شغال بـ `nohup` مش بيقف |
| Connection timed out في SSH | الـ IP بتاع النت عندك اتغير | EC2 > Security Groups > حدّث rule الـ SSH لـ My IP |
| `docker: permission denied` | لسه ما خرجتش ودخلت بعد سكريبت الإعداد | `exit` وادخل تاني |
| swap اتعمل 8 جيجا | السيرفر بيبلّغ عن 7.6 جيجا رامات | اتصلح في السكريبت |
| اللصق في السيرفر مش بيعمل حاجة | ما اتضغطش Enter | كليك يمين للصق، وبعدين Enter |
| Caddy مش شايف تعديل الـ Caddyfile | الـ container لسه على النسخة القديمة | `docker compose up -d --force-recreate caddy` |
| `git pull` بيقول `divergent branches` | حد عمل force push على GitHub | `git fetch origin && git reset --hard origin/main` (مش بيلمس `.env`) |
| Claude: `tool_choice ... not supported` | الموديلات الجديدة مش بتقبل forced tool use | استخدمنا structured output (`output_config`) |
| Publish app مقفول في جوجل | ناقص privacy policy | صفحة `/privacy` مفتوحة من غير باسورد |
| جوجل طلب verification | اترفع لوجو في Branding | امسح اللوجو |
| ربط يوتيوب بيقع بعد أسبوع | التطبيق لسه Testing | Publish app لحد ما يبقى In production |
| لينك Zoom في "Paste a link" فشل | yt-dlp مش بيعرف يجيب ترانسكريبت Zoom، و WhisperX محتاج `HF_TOKEN` | استخدم تاب **From Zoom** |
| لينكات يوتيوب مش بتتحمّل من السيرفر | يوتيوب بيعمل block لـ IPs الداتا سنترز | ارفع الملف نفسه |
| ميتنج رمادي في From Zoom | Zoom لسه بيعالجه، أو مفيش ترانسكريبت | استنى شوية واضغط Refresh، أو فعّل Create audio transcript |
| `accounts/me/recordings: HTTP 400` في zoom-check | ناقص scope الحساب كله | طبيعي، الأداة بتستخدم `users/me` |

## 20. أمان وتكلفة

### Checklist أمان

- [ ] الـ SSH مفتوح لـ My IP بس
- [ ] الديسك Encrypted
- [ ] الـ app مش ظاهر على الإنترنت مباشرة، Caddy بس اللي يوصله
- [ ] الموقع كله ورا باسورد، ما عدا `/privacy`
- [ ] `.env` و `caddy.env` صلاحياتهم 600، وموجودين في `.gitignore`
- [ ] الـ deploy key للقراية بس
- [ ] التحديثات الأمنية الأوتوماتيك شغالة (unattended-upgrades)
- [ ] Budget alarm معمول
- [ ] أي مفتاح اتبعت في شات أو إيميل أو سكرين شوت يتغيّر، والجديد يتحط على السيرفر بـ `read -s`

### التكلفة التقريبية في الشهر

| البند | التكلفة |
|---|---|
| EC2 `t3.large` شغال 24 ساعة | حوالي 70$ (0.096$ في الساعة) |
| ديسك 30 GiB gp3 | حوالي 3$ |
| Elastic IP مربوط بالسيرفر | حوالي 4$ |
| Claude (تحليل الميتنج) | حسب الاستخدام، كام سنت لكل ميتنج |
| Google Cloud و YouTube API و Zoom API | مجاني |
| الدومين و HTTPS | موجودين أصلاً، و Let's Encrypt مجاني |

الأرقام تقريبية، راجع AWS Pricing Calculator للرقم الدقيق. ولو عاوز توفّر، ممكن توقف السيرفر بالليل (Stop من EC2): الـ Elastic IP والداتا بيفضلوا، والتكلفة بتنزل للديسك والـ IP بس.
