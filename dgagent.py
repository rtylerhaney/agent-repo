# dgagent.py

from dotenv import load_dotenv
load_dotenv()

import os
import sqlite3
import feedparser
import requests
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== CONFIGURATION =====
FEEDS = {
    "DemandGenReport": "https://www.demandgenreport.com/feed/",
    "MarketingProfs":  "https://www.marketingprofs.com/topic/all/rss",
    "TopRank":         "http://feeds.feedburner.com/onlinemarketingseoblog",
    "Forrester":       "https://go.forrester.com/blogs/feed/",
    "CMOPodcast":      "https://rss.art19.com/the-cmo-podcast",
    "RevOpsCoOp":      "https://revopscoop.substack.com/feed",
    "WizardsOfOps":    "https://wizardofops.substack.com/feed",
}

DB_PATH        = os.getenv("DB_PATH", "seen_dg_articles.db")
EMAIL_FROM     = os.getenv("EMAIL_FROM", "730grand@gmail.com")
RECIPIENTS     = ["tyler.haney@gmail.com", "katiekregel@gmail.com"]

SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", 587))
SMTP_USER      = os.getenv("SMTP_USER", "730grand@gmail.com")
SMTP_PASS      = os.getenv("SMTP_PASS", "m1nn3$0TaB1rCH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ===== INITIALIZE OPENAI CLIENT =====
client = OpenAI(api_key=OPENAI_API_KEY)

# ===== DATABASE SETUP =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            url TEXT PRIMARY KEY,
            title TEXT,
            date TEXT,
            summary TEXT
        )
    """)
    return conn

# ===== FETCH NEW ARTICLES (limit 5 per source) =====
def fetch_new_items(conn):
    new_items_by_source = {}
    for source, url in FEEDS.items():
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            feed = feedparser.parse(resp.content)
            entries = feed.entries[:5]
        except Exception:
            entries = []
        items = []
        for entry in entries:
            link = entry.get("link") or (entry.links[0].get("href") if entry.get("links") else None)
            if not link or conn.execute("SELECT 1 FROM seen WHERE url=?", (link,)).fetchone():
                continue
            items.append({
                "source": source,
                "title":  entry.get("title", "No title"),
                "url":    link,
                "excerpt": entry.get("summary", "")[:300],
                "date":    entry.get("published", "")
            })
        new_items_by_source[source] = items
    return new_items_by_source

# ===== SUMMARIZE WITH OPENAI =====
def summarize_article(article):
    prompt = (
        "Summarize this article in two sentences, focusing on the core demand-gen takeaway:\n\n"
        f"{article['title']}\n{article['url']}\n{article['excerpt']}"
    )
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role":"user","content":prompt}]
    )
    return resp.choices[0].message.content.strip()

def summarize_tldr(summaries):
    prompt = (
        "Given these demand-generation article summaries, provide a detailed summary in 4–5 concise paragraphs, "
        "highlighting key themes, emerging trends, and actionable insights across the batch of articles:\n\n"
        + "\n".join(f"- {s}" for s in summaries)
    )
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role":"user","content":prompt}]
    )
    return resp.choices[0].message.content.strip()

# ===== EMAIL DIGEST =====
def send_email(new_items_by_source):
    date_str = datetime.now().strftime("%Y-%m-%d")
    # collect all summaries for TLDR
    all_summaries = []
    for items in new_items_by_source.values():
        for itm in items:
            all_summaries.append(f"{itm['title']}: {itm['summary']}")

    tldr = summarize_tldr(all_summaries) if all_summaries else "No new articles from any source today."

    html_parts = [
        f"<h2>Daily Demand Gen Digest – {date_str}</h2>",
        f"<h3>TLDR Summary</h3><div>{tldr.replace(chr(10), '<br><br>')}</div>"
    ]

    for source in FEEDS:
        items = new_items_by_source.get(source, [])
        if items:
            html_parts.append(f"<h4>{source}</h4>")
            for itm in items:
                html_parts.append(
                    f"<p><a href='{itm['url']}'>{itm['title']}</a><br>{itm['summary']}</p>"
                )
        else:
            html_parts.append(f"<p><strong>{source}:</strong> No articles today.</p>")

    html_body = "<html><body>" + "".join(html_parts) + "</body></html>"

    msg = MIMEText(html_body, "html")
    msg["Subject"] = f"Daily Demand Gen Digest – {date_str}"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(RECIPIENTS)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.sendmail(EMAIL_FROM, RECIPIENTS, msg.as_string())

# ===== MAIN PROCESS =====
def main():
    conn = init_db()
    new_items_by_source = fetch_new_items(conn)

    any_new = False
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_item = {}
        for items in new_items_by_source.values():
            for itm in items:
                future = pool.submit(summarize_article, itm)
                future_to_item[future] = itm

        for future in as_completed(future_to_item):
            itm = future_to_item[future]
            try:
                itm["summary"] = future.result()
            except Exception as e:
                itm["summary"] = f"[Error summarizing: {e}]"
            conn.execute(
                "INSERT INTO seen (url, title, date, summary) VALUES (?,?,?,?)",
                (itm["url"], itm["title"], itm["date"], itm["summary"])
            )
            any_new = True

    conn.commit()

    if any_new:
        send_email(new_items_by_source)
        print("Sent digest with extended TLDR and per-source updates.")
    else:
        print("No new articles today for any source.")

if __name__ == "__main__":
    main()