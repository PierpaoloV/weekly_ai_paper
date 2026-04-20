import os, smtplib, html, json, requests, feedparser, yaml, re, time, csv, pandas as pd
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
import openai


# ---------- Reviewer personas ----------

REVIEWER_A_PERSONA = """You are a strict specialist reviewer. Your bar is high: only papers \
that directly address the researcher's exact diseases, tasks, or methods earn a high score. \
Default to skepticism — look for reasons to reject. Tangential relevance, weak baselines, \
or incremental contributions are grounds for a low score."""

REVIEWER_B_PERSONA = """You are a generous generalist reviewer. Your goal is to surface papers \
the researcher might otherwise overlook. A paper earns a high score if its methods, findings, \
or framing could transfer to or inform the researcher's work, even if the domain does not match \
exactly. Default to inclusion — look for reasons why this paper could matter."""


# ---------- Reviewer A & B: per-paper scoring ----------

def review_paper(item, profile, persona):
    title = item.get("title") or ""
    abstract = item.get("abstract") or ""

    prompt = f"""
{persona}

You are evaluating a paper for a researcher with the following profile:

{profile}

Title: {title}
Abstract: {abstract}

Task:
1. Write a concise summary (4–6 sentences) from your reviewer perspective.
2. Assign two scores:
   - "domain_score" (0–10): how clearly the paper belongs to the researcher's domain.
   - "personal_score" (0–10): how relevant it is to the researcher's specific focus, judged from your reviewer perspective.
3. Write a "comment" (2–3 sentences) explaining your scores and perspective.
4. Output strictly valid JSON with keys: "summary", "domain_score", "personal_score", "comment".
"""
    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        content = resp.choices[0].message.content.strip()
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            result = json.loads(match.group(0)) if match else {
                "summary": content[:500], "domain_score": 0, "personal_score": 0, "comment": "Parsing failed."
            }
        domain = float(result.get("domain_score", 0))
        personal = float(result.get("personal_score", 0))
        combined = round(0.6 * domain + 0.4 * personal, 2)
        return {
            "summary": result.get("summary", "").strip(),
            "domain_score": domain,
            "personal_score": personal,
            "combined_score": combined,
            "comment": result.get("comment", "").strip(),
        }
    except Exception as e:
        print(f"⚠️ Review failed: {e}")
        return {"summary": "N/A", "domain_score": 0, "personal_score": 0, "combined_score": 0, "comment": "Error"}


def run_reviewer(items, profile, persona, label):
    print(f"Running {label}...")
    results = []
    for it in items:
        res = review_paper(it, profile, persona)
        entry = dict(it)
        entry.update({f"{label}_{k}": v for k, v in res.items()})
        results.append(entry)
    print(f"{label} complete.")
    return results


# ---------- Editor: arbitration ----------

def editorial_review(reviews_a, reviews_b, profile):
    papers_text = ""
    for i, (a, b) in enumerate(zip(reviews_a, reviews_b)):
        week_tag = f"  Week: {a.get('week', 'unknown')}"
        papers_text += f"""
---
Paper {i+1}: {a.get('title', '')}
{week_tag}

Reviewer A (specialist-skeptic):
  Score: {a.get('reviewer_a_combined_score', 0):.1f}
  Summary: {a.get('reviewer_a_summary', '')}
  Comment: {a.get('reviewer_a_comment', '')}

Reviewer B (generalist-advocate):
  Score: {b.get('reviewer_b_combined_score', 0):.1f}
  Summary: {b.get('reviewer_b_summary', '')}
  Comment: {b.get('reviewer_b_comment', '')}
"""

    prompt = f"""
You are the editor of a weekly AI paper digest.

The researcher you serve has the following profile:
{profile}

A specialist-skeptic reviewer (A) and a generalist-advocate reviewer (B) have independently evaluated a pool of papers.
The pool may include papers from this week and leftover candidates from the previous week.

Your job is to produce a final curated and ranked list.

Instructions:
- Only include papers with a genuine final_score of 7 or above. Do not pad.
- Select at most 7 papers. Fewer is better than including a mediocre paper.
- If both reviewers agree a paper is relevant: confirm and justify the consensus.
- If reviewers disagree: decide which perspective better serves this researcher and explain why.
- Prefer this week's papers over previous week's when quality is equal.
- Rank from most to least relevant.
- For each selected paper return:
  - "index": the paper number from the list above (integer)
  - "final_score": 1–10
  - "verdict": one of "consensus", "reviewer_a", "reviewer_b"
  - "summary": best available summary (pick from A, pick from B, or synthesize)
  - "reasoning": 2–3 sentences explaining your editorial decision

Return a JSON array only. No extra text.

Papers to evaluate:
{papers_text}
"""
    try:
        resp = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = resp.choices[0].message.content.strip()
        try:
            final_list = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", content, re.DOTALL)
            final_list = json.loads(match.group(0)) if match else []
    except Exception as e:
        print(f"⚠️ Editorial review failed: {e}")
        return []

    merged = []
    for ed in final_list:
        idx = ed.get("index")
        if idx is None or not (1 <= int(idx) <= len(reviews_a)):
            print(f"⚠️ Editor returned invalid index: {idx}")
            continue
        original = reviews_a[int(idx) - 1]
        merged.append({
            "title":       original.get("title"),
            "authors":     original.get("authors"),
            "venue":       original.get("venue"),
            "date":        original.get("date"),
            "doi":         original.get("doi"),
            "url":         original.get("url"),
            "summary":     ed.get("summary", ""),
            "final_score": float(ed.get("final_score", 0)),
            "verdict":     ed.get("verdict", ""),
            "reasoning":   ed.get("reasoning", ""),
        })

    merged.sort(key=lambda x: x["final_score"], reverse=True)
    print(f"Editorial review complete — {len(merged)} papers selected.")
    return merged


# ---------- Utils ----------

def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_researcher_profile(path="researcher_profile.md"):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def now_utc(): return datetime.now(timezone.utc)
def dt_to_str(dt): return dt.strftime("%Y-%m-%d")
def clip(s, n=600): return s if s and len(s) <= n else s[:n].rstrip() + "…" if s else None
def contains_any(text, words): return any(w.lower() in (text or "").lower() for w in words)
def strip_html(text): return re.sub(r'<[^>]+>', ' ', text or '').strip()


# ---------- Sources (PubMed + arXiv) ----------

def query_pubmed(cfg):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    since = now_utc() - timedelta(days=cfg["search"]["days_back"])
    or_block = " OR ".join([f'"{k}"[All Fields]' for k in cfg["search"]["keywords_any"]])
    date_q = f'("{dt_to_str(since)}"[Date - Publication] : "3000"[Date - Publication])'
    full_q = f"({or_block}) AND {date_q}"

    params = {"db": "pubmed", "term": full_q, "retmax": cfg["search"]["max_results"], "retmode": "json", "sort": "pub+date"}
    esearch = requests.get(base + "esearch.fcgi", params=params, timeout=30).json()
    ids = esearch.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    esum = requests.get(base + "esummary.fcgi", params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"}, timeout=30).json()
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
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pid}/"
        try:
            time.sleep(0.34)  # NCBI rate limit: 3 req/s without API key
            efetch = requests.get(base + "efetch.fcgi", params={"db": "pubmed", "id": pid, "rettype": "abstract", "retmode": "text"}, timeout=30).text
            abst = clip(efetch.strip(), 1000)
        except Exception:
            abst = None
        out.append({"source": "PubMed", "title": title, "authors": authors, "venue": journal, "date": pubdate, "doi": doi, "url": url, "abstract": abst})
    return out

def query_arxiv(cfg):
    feeds = ["http://export.arxiv.org/rss/cs.CV", "http://export.arxiv.org/rss/cs.LG", "http://export.arxiv.org/rss/eess.IV", "http://export.arxiv.org/rss/q-bio.QM"]
    since = now_utc() - timedelta(days=cfg["search"]["days_back"])
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
            summary = strip_html(html.unescape(getattr(e, "summary", "") or ""))
            link = e.link
            authors = ", ".join([a.get("name", "") for a in getattr(e, "authors", [])][:8])
            items.append({"source": "arXiv", "title": title, "authors": authors, "venue": "arXiv", "date": dt_to_str(published), "doi": None, "url": link, "abstract": clip(summary, 1000)})
    kw_any = cfg["search"]["keywords_any"]
    return [it for it in items if contains_any(it["title"] + " " + (it["abstract"] or ""), kw_any)]

def dedup(items):
    seen = set()
    out = []
    for it in items:
        key = (it.get("doi") or "").lower() or it.get("title", "").strip().lower()
        if key and key not in seen:
            out.append(it)
            seen.add(key)
    return out


# ---------- Sent-paper tracking ----------

def load_sent_papers(path="sent_papers.json"):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(d.lower() for d in json.load(f))
    except Exception:
        return set()

def save_sent_papers(sent_ids, path="sent_papers.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent_ids)), f, ensure_ascii=False, indent=2)

def get_paper_id(it):
    return (it.get("doi") or it.get("title") or "").strip().lower()

def load_candidate_pool(path="candidate_pool.json"):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_candidate_pool(candidates, path="candidate_pool.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

LOG_FIELDS = ["date_sent", "week", "title", "authors", "venue", "doi", "url",
              "final_score", "verdict", "reasoning", "summary"]

def append_sent_log(papers, date_sent, week, path="sent_papers_log.csv"):
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for p in papers:
            writer.writerow({**p, "date_sent": date_sent, "week": week})


# ---------- Email ----------

def build_email_html(picks):
    today = datetime.now().strftime("%d %B %Y")
    n_paper = len(picks)

    intro = f"""
    <p style="font-size:15px; color:#222;">
      Here are <b>{n_paper}</b> papers selected for you this week (<b>{today}</b>):
    </p>
    """

    verdict_labels = {
        "consensus":  ("✓ Consensus",       "#2e7d32"),
        "reviewer_a": ("🔬 Specialist pick", "#1565c0"),
        "reviewer_b": ("🌐 Generalist pick", "#6a1b9a"),
    }

    rows = []
    for i, p in enumerate(picks, 1):
        authors  = html.escape(p.get("authors") or "")
        venue    = html.escape(p.get("venue") or "")
        title    = html.escape(p.get("title") or "")
        url      = p.get("url") or "#"
        doi      = p.get("doi")
        doi_html = f' &middot; DOI: <a href="https://doi.org/{html.escape(doi)}">{html.escape(doi)}</a>' if doi else ""
        summary  = html.escape(p.get("summary") or "")
        reasoning = html.escape(p.get("reasoning") or "")
        score    = float(p.get("final_score", 0))
        verdict  = p.get("verdict", "")
        v_label, v_color = verdict_labels.get(verdict, ("", "#555"))

        rows.append(f"""
        <tr>
          <td style="padding:14px; border-bottom:1px solid #eee;">
            <div style="font-size:16px; font-weight:600; margin-bottom:4px;">
              {i}. <a href="{url}">{title}</a>
            </div>
            <div style="color:#555; margin-bottom:4px;">{authors}</div>
            <div style="color:#777; font-size:13px; margin-bottom:8px;">{venue}{doi_html}</div>
            <div style="font-size:14px; color:#222; line-height:1.5;">
              <b>Score:</b> {score:.1f} &nbsp;
              <span style="color:{v_color}; font-weight:600;">{v_label}</span><br><br>
              <i>{summary}</i><br><br>
              <b>Editor's note:</b> {reasoning}
            </div>
          </td>
        </tr>
        """)

    table_html = "<table style='width:100%; border-collapse:collapse;'>" + "\n".join(rows) + "</table>"

    return f"""
    <div style="font-family:system-ui,Segoe UI,Arial; max-width:820px; margin:auto;">
      <h2 style="font-weight:700;">Weekly Highlights — AI Paper Digest</h2>
      {intro}
      {table_html}
      <p style="color:#999; font-size:12px; margin-top:16px;">
        Generated automatically via GitHub Actions.
      </p>
    </div>
    """


def send_email(cfg, html_body):
    email_from = os.environ[cfg["email"]["from_env"]]
    email_to = [a.strip() for a in os.environ[cfg["email"]["to_env"]].split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = cfg["email"]["subject"]
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if cfg["email"]["send_via"] == "gmail":
        user = os.environ[cfg["email"]["gmail_user_env"]]
        pw = os.environ[cfg["email"]["gmail_app_password_env"]]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.sendmail(email_from, email_to, msg.as_string())
    else:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "localhost"), int(os.environ.get("SMTP_PORT", "25"))) as s:
            if os.environ.get("SMTP_USER"):
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.sendmail(email_from, email_to, msg.as_string())


# ---------- Main ----------

def main():
    cfg = load_config()
    profile = load_researcher_profile()
    openai.api_key = os.environ.get("OPENAI_API_KEY")

    pubmed = query_pubmed(cfg)
    arx = query_arxiv(cfg)
    items = dedup(pubmed + arx)

    sent_ids = load_sent_papers()
    candidate_pool = load_candidate_pool()
    this_week = dt_to_str(now_utc())

    new_items = [it for it in items if get_paper_id(it) not in sent_ids]
    print(f"Found {len(items)} total papers, {len(new_items)} new this week, {len(candidate_pool)} in candidate pool.")

    if not new_items and not candidate_pool:
        print("No papers to process.")
        return

    # Run reviewers on this week's new papers
    reviews_a, reviews_b = [], []
    if new_items:
        reviews_a = run_reviewer(new_items, profile, REVIEWER_A_PERSONA, "reviewer_a")
        reviews_b = run_reviewer(new_items, profile, REVIEWER_B_PERSONA, "reviewer_b")
        for r in reviews_a + reviews_b:
            r["week"] = this_week
        pd.DataFrame(reviews_a).to_csv("reviewer_a_results.csv", index=False)
        pd.DataFrame(reviews_b).to_csv("reviewer_b_results.csv", index=False)

    # Merge with candidate pool (previous week's unsent papers, already scored)
    combined_a = reviews_a + candidate_pool
    combined_b = reviews_b + candidate_pool

    # Editor — arbitration across this week + pool
    final_picks = editorial_review(combined_a, combined_b, profile)

    if not final_picks:
        print("⚠️ Editorial review returned no papers — falling back to Reviewer A scores.")
        fallback = sorted(reviews_a, key=lambda x: x.get("reviewer_a_combined_score", 0), reverse=True)
        final_picks = fallback[:7]

    top = final_picks[:7]

    html_email = build_email_html(top)
    send_email(cfg, html_email)
    print(f"Sent {len(top)} papers.")

    # Update sent papers
    sent_paper_ids = {get_paper_id(p) for p in top}
    for it in top:
        sent_ids.add(get_paper_id(it))
    save_sent_papers(sent_ids)
    append_sent_log(top, date_sent=this_week, week=this_week)

    # Update candidate pool: this week's unsent + previous pool's unsent, capped to 1 week back
    one_week_ago = dt_to_str(now_utc() - timedelta(days=7))
    this_week_unsent = [
        {**a, **{k: v for k, v in b.items() if k.startswith("reviewer_b_")}}
        for a, b in zip(reviews_a, reviews_b)
        if get_paper_id(a) not in sent_paper_ids
    ]
    pool_unsent = [
        p for p in candidate_pool
        if get_paper_id(p) not in sent_paper_ids and p.get("week", "") >= one_week_ago
    ]
    save_candidate_pool(pool_unsent + this_week_unsent)
    print(f"Candidate pool updated: {len(pool_unsent + this_week_unsent)} papers carried forward.")


if __name__ == "__main__":
    main()
