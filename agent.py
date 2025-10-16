import os, smtplib, html, json, requests, feedparser, yaml, csv, re, pandas as pd
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
import openai


# ---------- Stage 1: Summarize + Score ----------
def summarize_and_score(item):
    abstract = item.get("abstract") or ""
    title = item.get("title") or ""

    text = f"""
You are assisting Pierpaolo Vendittelli, a postdoctoral researcher in Artificial Intelligence and Computational Pathology.

His expertise includes:
- Deep learning for digital histopathology
- Subtyping, segmentation, detection, classification, and survival prediction
- Weakly-supervised learning, Multiple Instance Learning (MIL), attention-based models
- Transformer architectures and foundation models for computational pathology

Your goal is to evaluate scientific papers for relevance to his research.

Title: {title}
Abstract: {abstract}

Task:
1. Write a concise but informative summary (5–7 sentences).
2. Evaluate two scores:
   - "domain_score" (0–10): how clearly the paper is about histopathology or computational pathology and AI.
   - "personal_score" (0–10): how relevant the paper is to Pierpaolo’s specific focus areas (subtyping, segmentation, detection, classification, survival, weakly-supervised learning, MIL, attention models, transformers, foundation models).
3. Provide a "comment" (2–3 sentences explaining your reasoning for the scores).
4. Output strictly in valid JSON with keys: "summary", "domain_score", "personal_score", "comment".
"""

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}],
            temperature=0.4,
        )
        content = resp.choices[0].message.content.strip()
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            result = json.loads(match.group(0)) if match else {
                "summary": content[:500],
                "domain_score": 0,
                "personal_score": 0,
                "comment": "Parsing failed."
            }

        domain_score = float(result.get("domain_score", 0))
        personal_score = float(result.get("personal_score", 0))
        combined = round(0.6 * domain_score + 0.4 * personal_score, 2)

        return {
            "summary": result.get("summary", "").strip(),
            "domain_score": domain_score,
            "personal_score": personal_score,
            "combined_score": combined,
            "comment": result.get("comment", "").strip(),
        }

    except Exception as e:
        print("⚠️ GPT summary failed:", e)
        return {"summary": "N/A", "domain_score": 0, "personal_score": 0, "combined_score": 0, "comment": "Error"}


# ---------- Stage 2: Re-Ranking ----------
def rerank_papers(stage1_path="stage1_results.csv", out_path="stage2_reranked.csv"):
    if not os.path.exists(stage1_path):
        print("⚠️ Stage1 results not found — skipping rerank.")
        return None

    df = pd.read_csv(stage1_path)
    if df.empty:
        print("⚠️ No papers to rerank.")
        return None

    papers_text = "\n\n".join(
        f"{i+1}. {r['title']} (domain={r['domain_score']}, personal={r['personal_score']})\n"
        f"Summary: {r['summary']}\nComment: {r['comment']}"
        for i, r in df.iterrows()
    )

    prompt = f"""
You are acting as a second-stage meta-reviewer.

Below are papers that have already been scored for relevance to computational pathology by another model.
Each paper has a title, summary, and initial scores (domain + personal).

Re-evaluate ALL of them **together** and produce a new JSON list.
For each paper include:
  - "title"
  - "revised_score" (1–10, stricter, relative to others)
  - "reason" (1–2 sentences explaining the revised score)

Scoring rules:
10 = directly about AI / deep learning models for pathology or histopathology
7–9 = strong AI component, but not pathology-specific
4–6 = peripheral or general AI with limited pathology focus
1–3 = unrelated to AI or pathology

Be stricter and apply relative ranking — only a few papers should score ≥9.

Return JSON only.
"""

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt + "\n\n" + papers_text}],
            temperature=0.2,
        )
        content = resp.choices[0].message.content.strip()
        try:
            revised = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", content, re.DOTALL)
            revised = json.loads(match.group(0)) if match else []
    except Exception as e:
        print("⚠️ GPT rerank failed:", e)
        return None

    # Merge revised scores back
    revised_df = pd.DataFrame(revised)
    merged = df.merge(revised_df, on="title", how="left")
    merged.to_csv(out_path, index=False)
    print(f"Stage 2 re-ranking complete — saved to {out_path}")
    return merged


# ---------- Utils ----------
def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def now_utc(): return datetime.now(timezone.utc)
def dt_to_str(dt): return dt.strftime("%Y-%m-%d")
def clip(s, n=600): return s if s and len(s)<=n else s[:n].rstrip()+"…" if s else None
def contains_any(text, words): return any(w.lower() in (text or "").lower() for w in words)


# ---------- Sources (PubMed + arXiv) ----------
def query_pubmed(cfg):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    since = now_utc() - timedelta(days=cfg["search"]["days_back"])
    or_block = " OR ".join([f'"{k}"[All Fields]' for k in cfg["search"]["keywords_any"]])
    date_q = f'("{dt_to_str(since)}"[Date - Publication] : "3000"[Date - Publication])'
    full_q = f"({or_block}) AND {date_q}"

    params = {"db":"pubmed","term":full_q,"retmax":cfg["search"]["max_results"],"retmode":"json","sort":"pub+date"}
    esearch = requests.get(base+"esearch.fcgi",params=params,timeout=30).json()
    ids = esearch.get("esearchresult",{}).get("idlist",[])
    if not ids: return []

    esum = requests.get(base+"esummary.fcgi",params={"db":"pubmed","id":",".join(ids),"retmode":"json"},timeout=30).json()
    out=[]; result=esum.get("result",{})
    for pid in ids:
        rec=result.get(pid,{}); 
        if not rec: continue
        title=rec.get("title",""); journal=rec.get("fulljournalname") or rec.get("source") or "PubMed"
        authors=", ".join([a.get("name") for a in rec.get("authors",[])][:8])
        pubdate=rec.get("pubdate") or rec.get("sortpubdate")
        doi=None
        for aid in rec.get("articleids",[]): 
            if aid.get("idtype")=="doi": doi=aid.get("value")
        url=f'https://pubmed.ncbi.nlm.nih.gov/{pid}/'
        try:
            efetch=requests.get(base+"efetch.fcgi",params={"db":"pubmed","id":pid,"rettype":"abstract","retmode":"text"},timeout=30).text
            abst=clip(efetch.strip(),1000)
        except Exception: abst=None
        out.append({"source":"PubMed","title":title,"authors":authors,"venue":journal,"date":pubdate,"doi":doi,"url":url,"abstract":abst})
    return out

def query_arxiv(cfg):
    feeds=["http://export.arxiv.org/rss/cs.CV","http://export.arxiv.org/rss/cs.LG","http://export.arxiv.org/rss/eess.IV","http://export.arxiv.org/rss/q-bio.QM"]
    since=now_utc()-timedelta(days=cfg["search"]["days_back"])
    items=[]
    for url in feeds:
        d=feedparser.parse(url)
        for e in d.entries:
            try: published=datetime(*e.published_parsed[:6],tzinfo=timezone.utc)
            except Exception: published=now_utc()
            if published<since: continue
            title=e.title; summary=html.unescape(getattr(e,"summary","") or ""); link=e.link
            authors=", ".join([a.get("name","") for a in getattr(e,"authors",[])][:8])
            items.append({"source":"arXiv","title":title,"authors":authors,"venue":"arXiv","date":dt_to_str(published),"doi":None,"url":link,"abstract":clip(summary,1000)})
    kw_any=cfg["search"]["keywords_any"]
    return [it for it in items if contains_any(it["title"]+" "+(it["abstract"] or ""),kw_any)]

def dedup(items):
    seen=set(); out=[]
    for it in items:
        key=(it.get("doi") or "").lower() or it.get("title","").strip().lower()
        if key and key not in seen: out.append(it); seen.add(key)
    return out


# ---------- Sent-paper tracking ----------
def load_sent_papers(path="sent_papers.json"):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(d.lower() for d in data)
    except Exception:
        return set()

def save_sent_papers(sent_ids, path="sent_papers.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent_ids)), f, ensure_ascii=False, indent=2)

def get_paper_id(it):
    """Generate a unique ID based on DOI or title."""
    return (it.get("doi") or it.get("title") or "").strip().lower()



# ---------- Email ----------
def build_email_html(picks):
    # === Calcola data e numero paper ===
    data_odierna = datetime.now().strftime("%d %B %Y")  # es: 16 ottobre 2025
    n_paper = len(picks)

    # === Messaggio iniziale ===
    intro_message = f"""
    <p style="font-size:15px; color:#222;">
      Ciao Pierpaolo, oggi è <b>{data_odierna}</b> e ho trovato queste <b>{n_paper}</b> pubblicazioni interessanti per te questa settimana:
    </p>
    """

    # === Tabella con i paper ===
    rows = []
    for i, p in enumerate(picks, 1):
        authors = html.escape(p.get("authors") or "")
        venue = html.escape(p.get("venue") or "")
        title = html.escape(p.get("title") or "")
        url = p.get("url")
        doi = p.get("doi")
        doi_html = f' &middot; DOI: <a href="https://doi.org/{html.escape(doi)}">{html.escape(doi)}</a>' if doi else ""
        summary = html.escape(p.get("summary") or "")
        comment = html.escape(p.get("comment") or "")
        revised = p.get("revised_score", p.get("combined_score", 0))

        rows.append(f"""
        <tr>
          <td style="padding:12px; border-bottom:1px solid #eee;">
            <div style="font-size:16px; font-weight:600; margin-bottom:4px;">
              {i}. <a href="{url}">{title}</a>
            </div>
            <div style="color:#555; margin-bottom:4px;">{authors}</div>
            <div style="color:#777; font-size:13px; margin-bottom:8px;">{venue}{doi_html}</div>
            <div style="font-size:14px; color:#222; line-height:1.4;">
              <b>Score:</b> {revised:.1f}<br><br>
              <i>{summary}</i><br><br>
              <b>Comment:</b> {comment}
            </div>
          </td>
        </tr>
        """)

    # === HTML finale ===
    table_html = "<table style='width:100%; border-collapse:collapse;'>" + "\n".join(rows) + "</table>"

    html_body = f"""
    <div style="font-family:system-ui,Segoe UI,Arial; max-width:820px; margin:auto;">
      <h2 style="font-weight:700;">Weekly Highlights — AI for (Computational) Pathology</h2>
      {intro_message}
      {table_html}
      <p style="color:#999; font-size:12px; margin-top:16px;">
        Generato automaticamente ogni lunedì tramite GitHub Actions.
      </p>
    </div>
    """
    return html_body


def send_email(cfg, html_body):
    msg=MIMEMultipart("alternative")
    msg["Subject"]=cfg["email"]["subject"]
    msg["From"]=cfg["email"]["from"]
    msg["To"]=", ".join(cfg["email"]["to"])
    msg.attach(MIMEText(html_body,"html","utf-8"))
    if cfg["email"]["send_via"]=="gmail":
        user=os.environ[cfg["email"]["gmail_user_env"]]
        pw=os.environ[cfg["email"]["gmail_app_password_env"]]
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(user,pw); s.sendmail(cfg["email"]["from"],cfg["email"]["to"],msg.as_string())
    else:
        with smtplib.SMTP(os.environ.get("SMTP_HOST","localhost"),int(os.environ.get("SMTP_PORT","25"))) as s:
            if os.environ.get("SMTP_USER"): s.starttls(); s.login(os.environ["SMTP_USER"],os.environ["SMTP_PASS"])
            s.sendmail(cfg["email"]["from"],cfg["email"]["to"],msg.as_string())


# ---------- Main ----------
def main():
    cfg=load_config()
    openai.api_key=os.environ.get("OPENAI_API_KEY")

    pubmed=query_pubmed(cfg)
    arx=query_arxiv(cfg)
    items=dedup(pubmed+arx)
    # === Filtra paper già inviati ===
    sent_ids = load_sent_papers()
    new_items = [it for it in items if get_paper_id(it) not in sent_ids]

    print(f"Found {len(items)} total papers, {len(new_items)} new this week.")

    items = new_items  # Continua con Stage 1 su questi

    # Stage 1
    results=[]
    for it in items:
        res=summarize_and_score(it)
        it.update(res)
        results.append(it)
    pd.DataFrame(results).to_csv("stage1_results.csv",index=False)
    print("Stage 1 complete — summaries and scores saved.")

    # Stage 2
    reranked=rerank_papers("stage1_results.csv","stage2_reranked.csv")
    if reranked is None or reranked.empty:
        top=sorted(results,key=lambda x:x["combined_score"],reverse=True)[:20]
    else:
        top=reranked.sort_values("revised_score",ascending=False).head(20).to_dict("records")

    html_email=build_email_html(top)
    send_email(cfg, html_email)
    print(f"Sent {len(top)} papers (re-ranked).")

        # === Aggiorna la lista di paper inviati ===
    for it in top:
        sent_ids.add(get_paper_id(it))
    save_sent_papers(sent_ids)
    print(f"Updated sent_papers.json with {len(sent_ids)} total unique papers.")



if __name__=="__main__":
    main()
