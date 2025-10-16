import os, smtplib, html, json, requests, feedparser, yaml
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from dateutil import tz
from urllib.parse import urlencode
import openai


# ---------- GPT Summarizer + Scorer ----------
def summarize_and_score(item):
    """Use GPT to summarize and score paper relevance to AI in pathology."""
    abstract = item.get("abstract") or ""
    title = item.get("title") or ""
    text = (
        f"Title: {title}\nAbstract: {abstract}\n\n"
        "Task: Summarize this paper in 2 concise sentences and rate from 1–10 "
        "how relevant it is to AI methods in medical or computational pathology. "
        "Respond **only** in valid JSON with keys 'summary' and 'score'."
    )

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}],
            temperature=0.4,
        )
        content = resp.choices[0].message.content.strip()

        # Try strict JSON first
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # Fallback: try to extract JSON substring from text
            import re
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                result = json.loads(match.group(0))
            else:
                # Last resort: create fake summary
                result = {"summary": content[:250], "score": 5.0}

        summary = result.get("summary", "").strip()
        score = float(result.get("score", 0))
        return summary, score

    except Exception as e:
        print("⚠️ GPT summary failed:", e)
        return "Summary unavailable (GPT error).", 0



# ---------- Utils ----------
def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def now_utc():
    return datetime.now(timezone.utc)

def dt_to_str(dt):
    return dt.strftime("%Y-%m-%d")

def clip(s, n=600):
    if s is None:
        return None
    return s if len(s) <= n else s[:n].rstrip() + "…"

def contains_any(text, words):
    if not text: return False
    t = text.lower()
    return any(w.lower() in t for w in words)


# ---------- Sources ----------
def query_pubmed(cfg):
    """Use E-utilities: ESearch -> ESummary -> optional EFetch for abstract."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    days = cfg["search"]["days_back"]
    since = now_utc() - timedelta(days=days)

    kw_any = cfg["search"]["keywords_any"]
    or_block = " OR ".join([f'"{k}"[All Fields]' for k in kw_any])
    date_q = f'("{dt_to_str(since)}"[Date - Publication] : "3000"[Date - Publication])'
    full_q = f"({or_block}) AND {date_q}"

    params = {
        "db": "pubmed",
        "term": full_q,
        "retmax": cfg["search"]["max_results"],
        "retmode": "json",
        "sort": "pub+date",
    }
    esearch = requests.get(base + "esearch.fcgi", params=params, timeout=30).json()
    ids = esearch.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    esum = requests.get(
        base + "esummary.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        timeout=30,
    ).json()

    out = []
    result = esum.get("result", {})
    for pid in ids:
        rec = result.get(pid, {})
        if not rec:
            continue
        title = rec.get("title", "")
        journal = rec.get("fulljournalname") or rec.get("source") or "PubMed"
        authors = ", ".join([a.get("name") for a in rec.get("authors", [])][:8])
        pubdate = rec.get("pubdate") or rec.get("sortpubdate")
        doi = None
        for aid in rec.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value")
        url = f'https://pubmed.ncbi.nlm.nih.gov/{pid}/'
        # Try to fetch abstract (text)
        abst = None
        try:
            efetch = requests.get(
                base + "efetch.fcgi",
                params={"db": "pubmed", "id": pid, "rettype": "abstract", "retmode": "text"},
                timeout=30
            ).text
            abst = clip(efetch.strip(), 1000)
        except Exception:
            abst = None

        out.append({
            "source": "PubMed",
            "title": title,
            "authors": authors,
            "venue": journal,
            "date": pubdate,
            "doi": doi,
            "url": url,
            "abstract": abst
        })
    return out


def query_arxiv(cfg):
    """Use arXiv RSS + keyword filter (cs.CV, cs.LG, eess.IV, q-bio.QM)."""
    feeds = [
        "http://export.arxiv.org/rss/cs.CV",
        "http://export.arxiv.org/rss/cs.LG",
        "http://export.arxiv.org/rss/eess.IV",
        "http://export.arxiv.org/rss/q-bio.QM",
    ]
    days = cfg["search"]["days_back"]
    since = now_utc() - timedelta(days=days)

    items = []
    for url in feeds:
        d = feedparser.parse(url)
        for e in d.entries:
            try:
                published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                published = now_utc()
            if published < since:
                continue
            title = e.title
            summary = html.unescape(getattr(e, "summary", "") or "")
            link = e.link
            authors = ", ".join([a.get("name", "") for a in getattr(e, "authors", [])][:8])
            items.append({
                "source": "arXiv",
                "title": title,
                "authors": authors,
                "venue": "arXiv",
                "date": dt_to_str(published),
                "doi": None,
                "url": link,
                "abstract": clip(summary, 1000),
            })
    kw_any = cfg["search"]["keywords_any"]
    filtered = [it for it in items if contains_any((it["title"] + " " + (it["abstract"] or "")), kw_any)]
    return filtered


# ---------- Ranking / Filtering ----------
def dedup(items):
    seen = set()
    out = []
    for it in items:
        key = (it.get("doi") or "").lower() or it.get("title", "").strip().lower()
        if not key or key not in seen:
            out.append(it)
            if key:
                seen.add(key)
    return out


# ---------- Email ----------
def build_email_html(picks):
    rows = []
    for i, p in enumerate(picks, 1):
        authors = html.escape(p.get("authors") or "")
        venue = html.escape(p.get("venue") or "")
        title = html.escape(p.get("title") or "")
        url = p.get("url")
        doi = p.get("doi")
        doi_html = f' &middot; DOI: <a href="https://doi.org/{html.escape(doi)}">{html.escape(doi)}</a>' if doi else ""
        summary = html.escape(p.get("summary") or "")
        score = p.get("_score", 0)

        rows.append(f"""
        <tr>
          <td style="padding:12px; border-bottom:1px solid #eee;">
            <div style="font-size:16px; font-weight:600; margin-bottom:4px;">
              {i}. <a href="{url}">{title}</a>
            </div>
            <div style="color:#555; margin-bottom:4px;">{authors}</div>
            <div style="color:#777; font-size:13px; margin-bottom:8px;">{venue}{doi_html}</div>
            <div style="font-size:14px; color:#222; line-height:1.4;">
              <b>Relevance score:</b> {score:.1f}/10<br>
              <i>{summary}</i>
            </div>
          </td>
        </tr>
        """)

    table = "<table style='width:100%; border-collapse:collapse;'>" + "\n".join(rows) + "</table>"
    header = """
    <div style="font-family:system-ui,Segoe UI,Arial; max-width:820px; margin:auto;">
      <h2 style="font-weight:700;">Weekly Highlights — AI for (Computational) Pathology</h2>
      <p style="color:#555;">Selection of the last 7 days (PubMed + arXiv), ranked by GPT relevance and summarized concisely.</p>
    """
    footer = """
      <p style="color:#999; font-size:12px; margin-top:16px;">
        Adjust keywords or scoring in <code>config.yaml</code>.
      </p>
    </div>
    """
    return header + table + footer


def send_email(cfg, html_body):
    subject = cfg["email"]["subject"]
    sender = cfg["email"]["from"]
    recipients = cfg["email"]["to"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if cfg["email"]["send_via"] == "gmail":
        user = os.environ[cfg["email"]["gmail_user_env"]]
        app_pw = os.environ[cfg["email"]["gmail_app_password_env"]]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, app_pw)
            s.sendmail(sender, recipients, msg.as_string())
    else:
        host = os.environ.get("SMTP_HOST", "localhost")
        port = int(os.environ.get("SMTP_PORT", "25"))
        user = os.environ.get("SMTP_USER")
        pw = os.environ.get("SMTP_PASS")
        with smtplib.SMTP(host, port) as s:
            if user and pw:
                s.starttls()
                s.login(user, pw)
            s.sendmail(sender, recipients, msg.as_string())


# ---------- Main ----------
def main():
    cfg = load_config()
    openai.api_key = os.environ.get("OPENAI_API_KEY")

    pubmed = query_pubmed(cfg)
    arx = query_arxiv(cfg)
    items = dedup(pubmed + arx)

    # Summarize and score with GPT
    for it in items:
        summary, score = summarize_and_score(it)
        it["summary"] = summary
        it["_score"] = score

    # Keep only relevant papers (score ≥ 6)
    items = [it for it in items if it["_score"] >= 6]

    # Sort by score
    items.sort(key=lambda x: x["_score"], reverse=True)

    # Keep top N
    top = items[:20]

    if not top:
        print("No relevant items found.")
        return

    html_email = build_email_html(top)
    send_email(cfg, html_email)
    print(f"Sent {len(top)} items.")


if __name__ == "__main__":
    main()
