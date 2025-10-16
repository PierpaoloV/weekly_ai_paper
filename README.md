# Weekly AI Pathology Digest

A GitHub Actions-powered agent that, every Monday, fetches recent papers on AI for (computational) pathology from PubMed and arXiv, ranks them, and emails you a compact HTML digest.

## Quick start

1. **Fork or clone** this repo.
2. Edit `config.yaml`:
   - Set your `email.from` and `email.to`.
3. Add repo **Secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_USER` = your Gmail address
   - `GMAIL_APP_PASSWORD` = a Gmail App Password (requires 2FA)
4. Trigger the workflow manually (**Actions → Run workflow**) or wait for Monday 08:00 (Europe/Amsterdam).

## Local test
```bash
pip install -r requirements.txt
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
python agent.py
```

## Notes
- Adjust keywords in `config.yaml` to your interests (PDAC, TSR, HoVer-Net, MIL/CLAM, survival, etc.).
- If you prefer SMTP instead of Gmail, set `send_via: "smtp"` and configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` as environment variables in the workflow.
