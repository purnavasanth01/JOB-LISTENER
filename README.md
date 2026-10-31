# JOB-LISTENER

Phone + email alert within minutes of a new **Spring / Summer / Fall 2027** software internship being posted.

| Listener | Watches | How |
|---|---|---|
| 1 – own sites | Amazon, Google, Microsoft, Netflix, Oracle, Goldman, Uber, Dell, JPMorgan, Amex, AMD, Rivian | their careers APIs |
| 2 – job systems | 45+ target companies on Workday, Greenhouse, Ashby, SmartRecruiters | public job-board APIs |
| 3 – checker | Simplify, vanshb03, Jobright GitHub lists | catches anything 1/2 missed (tagged `MISSED by L1/L2`) and covers Meta, Apple, Tesla, TikTok, etc. |

Targets live in `targets.json` (H-1B sponsors, ranked). Filters live in `rules.py`.

## What gets alerted
- Any software-type internship: SWE, full stack, backend, frontend, data, AI/ML, cloud, DevOps, QA/automation, security, UI/UX, FDE, PM/TPM, research (not PhD-only), data/BI analyst, bank technology analyst.
- Excluded roles: embedded/hardware, semiconductor, robotics/autonomy, GPU/performance/compilers, quant/trading, sales/solutions/support, IT/sysadmin, non-tech.
- Terms: any 2027 term, or no term listed. US locations (or unknown).
- **Dropped only if** the posting requires US citizenship / green card / asylee status / security clearance, or is PhD-only. Everything else is kept and tagged (`⚠ no sponsorship`, `⚠ F-1/CPT excluded?`, `✓ F-1/CPT ok`, `⚠ export-control`, target flags).

## Files (on the `state` branch, updated every run)
- `backlog.csv` – everything already open when the listener first saw each source (go through these).
- `alerts.csv` – every alert sent. `missed.csv` – jobs only Listener 3 caught.
- `dropped.csv` – every new job that was filtered out, with the reason (audit this now and then).
- `last_run.log` – what the latest run did. `health.json` – per-source status.

## Setup (one time)
1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `NTFY_TOPIC` – your ntfy topic (subscribe to the same topic in the ntfy phone app).
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
