# JOB-LISTENER

Phone + email alert within minutes of a new **Spring / Summer / Fall 2027** software internship being posted.

| Listener | Watches | How |
|---|---|---|
| 1 – own sites | Amazon, Google, Microsoft, Netflix, BCG, plus Oracle Cloud sites (Oracle, Goldman, Uber, Dell, JPMorgan, Amex, Cummins, Hexaware) | their careers APIs |
| 2 – job systems | ~60 target companies on Workday, Greenhouse, Ashby, SmartRecruiters, Phenom | public job-board APIs |
| 3 – checker | Simplify, vanshb03, Jobright GitHub lists | catches anything 1/2 missed (tagged `MISSED by L1/L2`), covers every other company, and is the only source for ~48 targets (Meta, Apple, Tesla, TikTok, TCS, Infosys, ...) |

Targets live in `targets.json`: the 121 H1B_LIST companies (H-1B sponsors, ranked; Rivian excluded). Filters live in `rules.py`.

## What gets alerted
- Any software-type internship: SWE, full stack, backend, frontend, data, AI/ML, cloud, DevOps, QA/automation, security, UI/UX, FDE, PM/TPM, research, data/BI analyst, bank technology analyst, plus robotics / embedded **software** and compilers.
- Excluded: pure hardware / embedded / robotics engineering, semiconductor, GPU/performance, quant/trading, sales/solutions/support, IT/sysadmin, non-tech.
- Vague titles ("Engineering Intern") are not dropped blindly: the description is read and the job is kept if it has 4+ distinct tech words (tagged 🔍).
- Terms: any 2027 term, or no term listed. US locations (or unknown).
- **Dropped** when the posting requires US citizenship / green card / asylee / clearance / US persons, mentions ITAR, says F-1/CPT/OPT not eligible, says no sponsorship "now or in the future" or no H-1B, says the role is not eligible for / does not offer visa sponsorship (any wording, including the list flag "Does Not Offer Sponsorship"), needs "permanent work authorization", is undergrad-only, or is PhD-only.
- Otherwise the alert quotes the posting's own sponsorship sentence (💬). 🎓 flags a graduation window that excludes May 2028.

## Telegram buttons
- 🔗 Apply · ⭐ Star (interested; resume will be made) · ❌ Remove (ignored, message deleted).
- All 121 targets get the loud 🔥 message; only the top 25 (`"autostar": true` in `targets.json`) are starred automatically; ⭐ never un-stars. Resume generation is currently off (`RESUMES_ENABLED = False` in `mac/resume_runner.py`). Taps are processed on the next check (~3 min).
- Starred jobs live in `stars.json` on the state branch.

## Files (on the `state` branch, updated every run)
- `backlog.csv` – everything already open when the listener first saw each source (go through these).
- `alerts.csv` – every alert sent. `missed.csv` – jobs only Listener 3 caught.
- `dropped.csv` – every new job that was filtered out, with the reason and the sentence that caused it.
- `borderline.csv` – only the judgment-call drops (eligibility wording, vague titles) for a second look.
- `last_run.log` – what the latest run did. `health.json` – per-source status.

## Setup (one time)
1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_TOKEN` – bot token from @BotFather. Then send your bot any message; the next run connects to your chat and replies "Connected". Commands: `/status`, `/help`.
   - optional `NTFY_TOPIC` – ntfy topic, if you also want ntfy pushes.
   - `SMTP_USER` – your Gmail address. `SMTP_PASS` – a Gmail **App Password** (Google Account → Security → 2-Step Verification → App passwords).
   - optional `EMAIL_TO` – where to send (defaults to `SMTP_USER`).
2. **Actions** tab → enable workflows → run `listen` once manually (first run is silent and builds `backlog.csv`).
3. **cron-job.org** job, every 3 minutes:
   - URL `https://api.github.com/repos/purnavasanth01/JOB-LISTENER/actions/workflows/listen.yml/dispatches`
   - Method `POST`, body `{"ref":"main"}`
   - Headers: `Authorization: Bearer <fine-grained token: this repo only, Actions read+write>`,
     `Accept: application/vnd.github+json`, `Content-Type: application/json`
   - Turn on cron-job.org's failure email.

## Local test
```
python3 -m unittest discover -s tests
python3 main.py --dry-run --only greenhouse:stripe      # fetch + filter one source, save nothing
python3 main.py --state-dir /tmp/s --no-notify          # full run, print alerts instead of sending
```

## Health alerts
- `❌ <source> failing since …` after 1 hour of errors (1 day for Microsoft/Google), and `✅ … working again`.
- Daily `✅ Job listener alive` summary after 9 AM Central.
- `❌ Job listener crashed` if the program itself fails. GitHub also emails failed runs.
