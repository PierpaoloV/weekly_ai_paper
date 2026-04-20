# Weekly AI Paper Digest

A self-hosted, fully automated research assistant that runs every week via GitHub Actions.
It fetches recent papers from PubMed and arXiv, evaluates them through a three-agent LLM
pipeline, and delivers a curated digest of up to 7 papers to your inbox — tailored to your
research profile.

---

## How it works

```
PubMed + arXiv
      │
      ▼  keyword filtering + deduplication
      │
      ├── Reviewer A (gpt-4o-mini) ── strict specialist
      │   Scores each paper against your exact diseases, tasks, and methods.
      │   Defaults to rejection.
      │
      ├── Reviewer B (gpt-4o-mini) ── generous generalist
      │   Scores each paper for transferability and adjacent relevance.
      │   Defaults to inclusion.
      │
      └── Editor (gpt-4o)
          Receives both proposals. Arbitrates disagreements using your researcher
          profile as grounding. Selects up to 7 papers scoring ≥7/10.
          Prefers this week's papers; falls back to last week's candidates if needed.
                │
                ▼
          HTML email with score, verdict badge, and editor's reasoning per paper
```

### Verdict badges

| Badge | Meaning |
|---|---|
| ✓ Consensus | Both reviewers agreed |
| 🔬 Specialist pick | Reviewer A won the dispute (directly on-topic) |
| 🌐 Generalist pick | Reviewer B won the dispute (transferable value) |

### Candidate pool fallback

Papers evaluated but not selected are saved to `candidate_pool.json`.
The following week they re-enter the editor's pool alongside fresh papers.
If a week is thin, the editor can promote a strong paper from the previous week.
The pool is capped at 1 week — papers older than that are dropped automatically.

### Long-term log

Every paper sent is appended to `sent_papers_log.csv` with full metadata
(score, verdict, editor reasoning, summary). After a year you have a dataset
for evaluating the agent's curation quality.

---

## Quick start

**1. Fork or clone this repo.**

**2. Create your researcher profile:**
```bash
cp template_researcher_profile.md researcher_profile.md
```
Edit `researcher_profile.md` with your role, expertise, research focus, and topics of
interest. This file is gitignored — your personal data never leaves your machine.

**3. Configure your search:**

Edit `config.yaml`:
- Adjust `search.keywords_any` to match your field
- Set `search.days_back` (default: 7)
- Set your preferred digest schedule (see [Schedule](#schedule))

**4. Add GitHub Secrets:**

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `RESEARCHER_PROFILE` | Full contents of your `researcher_profile.md` (see below) |
| `EMAIL_FROM` | Sender string, e.g. `My Agent <you@gmail.com>` |
| `EMAIL_TO` | Recipient(s), comma-separated, e.g. `you@work.com` |
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | A Gmail App Password (requires 2FA enabled) |
| `OPENAI_API_KEY` | Your OpenAI API key |

**Setting `RESEARCHER_PROFILE`:**

Copy the template, fill it in locally, then paste the entire file content into the secret:

```bash
cp template_researcher_profile.md researcher_profile.md
# edit researcher_profile.md with your details
cat researcher_profile.md   # copy this output into the GitHub Secret
```

The workflow writes this secret to `researcher_profile.md` at runtime before the agent
runs. The file is never committed — your personal data stays in GitHub Secrets only.

**5. Trigger a first run:**

Go to **Actions → Weekly AI Pathology Digest → Run workflow**, or wait for the
next scheduled Monday run.

---

## Schedule

The digest runs every Monday at 08:00 UTC by default. To change the time:

1. Edit `config.yaml` — update `schedule.day`, `schedule.time`, `schedule.timezone`,
   and `schedule.cron_utc`
2. Edit `.github/workflows/weekly.yml` — update the `cron:` line to match

Use [crontab.guru](https://crontab.guru) to convert your local time to UTC.

---

## Local development

```bash
pip install -r requirements.txt

# Profile is written automatically by the workflow in CI.
# For local runs, create it manually (gitignored — never committed):
cp template_researcher_profile.md researcher_profile.md
# edit researcher_profile.md with your details

export OPENAI_API_KEY="sk-..."
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export EMAIL_FROM="My Agent <you@gmail.com>"
export EMAIL_TO="you@work.com"

python agent.py
```

Debug CSVs (`reviewer_a_results.csv`, `reviewer_b_results.csv`) are written locally
after each run so you can inspect the reviewers' raw scores. These are gitignored.

---

## Repository structure

```
.
├── agent.py                      # Main pipeline
├── config.yaml                   # Search settings and schedule
├── template_researcher_profile.md  # Profile template (commit this)
├── researcher_profile.md         # Your profile (gitignored, never committed)
├── requirements.txt
├── sent_papers.json              # Tracks sent paper IDs (auto-updated)
├── candidate_pool.json           # Last week's unsent candidates (auto-updated)
├── sent_papers_log.csv           # Full historical log of sent papers (auto-updated)
└── .github/workflows/weekly.yml  # GitHub Actions workflow
```

---

## Customisation

### Changing the search scope

Edit `search.keywords_any` in `config.yaml`. Papers are included if any keyword
matches the title or abstract. `block_terms` filters out unwanted matches.

### Using SMTP instead of Gmail

In `config.yaml` set `send_via: "smtp"` and add the following secrets:

| Secret | Value |
|---|---|
| `SMTP_HOST` | Your SMTP server hostname |
| `SMTP_PORT` | Port (typically `587`) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password |

### Adjusting the number of papers

The editor selects **at most 7 papers, only if score ≥ 7**. To change the cap,
edit the `top = final_picks[:7]` line in `main()` and update the editor prompt
accordingly.

---

## Cost

Both reviewers use `gpt-4o-mini` (per-paper scoring). The editor uses `gpt-4o`
(one call per week over the full pool). With 40 papers/week and standard pricing,
typical weekly cost is well under $0.10.

---

## Privacy

- `researcher_profile.md` is gitignored and never committed
- Email addresses are stored as GitHub Secrets, not in any committed file
- No paper data is stored outside your own repo and your OpenAI API calls
