#!/usr/bin/env python3
"""
Viral News Bot
--------------
Checks a couple of "what's trending right now" sources (Reddit r/popular,
Google Trends), picks the top story that hasn't been posted before, writes
a short neutral summary (via the Claude API, if configured) and posts it
to a Telegram channel.
 
Designed to be run on a schedule (cron, GitHub Actions, etc.) — each run
does one check-and-maybe-post cycle, then exits.
 
Required env vars:
    TELEGRAM_BOT_TOKEN   - from @BotFather
    TELEGRAM_CHAT_ID     - e.g. "@cnews5" (public channel username) or a
                           numeric chat id for a private channel
 
Optional env vars:
    ANTHROPIC_API_KEY    - if set, Claude writes a short neutral take on
                           the story. If not set, a plain template is used
                           instead (no AI call).
    CLAUDE_MODEL         - which Claude model to call. Check
                           https://docs.claude.com/en/docs/about-claude/models
                           for current model IDs — one isn't hardcoded here
                           on purpose, since model IDs change over time.
    STATE_FILE           - path to the JSON file used to avoid re-posting
                           the same story (default: posted.json, next to
                           this script)
"""
 
import hashlib
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
 
import requests
 
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted.json"))
 
STATE_MAX_AGE_DAYS = 30
REQUEST_TIMEOUT = 15
USER_AGENT = "viral-news-bot/1.0 (personal project)"
 
 
def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)
 
 
# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
 
def fetch_reddit_popular(limit=15):
    """Top posts from r/popular in the last hour."""
    url = f"https://www.reddit.com/r/popular/top.json?t=hour&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"reddit fetch failed: {e}")
        return []
 
    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = d.get("title")
        if not title:
            continue
        items.append({
            "id": "reddit:" + d.get("id", title),
            "title": title,
            "url": "https://reddit.com" + d.get("permalink", ""),
            "score": d.get("score", 0),
            "source": f"Reddit r/{d.get('subreddit', 'popular')}",
        })
    return items
 
 
GTRENDS_NS = {"ht": "https://trends.google.com/trending/rss"}
 
 
def _parse_approx_traffic(text):
    """'500+' -> 500, '10,000+' -> 10000, anything unparseable -> 0."""
    if not text:
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0
 
 
def fetch_google_trends(geo="US"):
    """Daily trending searches from Google Trends RSS."""
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        log(f"google trends fetch failed: {e}")
        return []
 
    items = []
    try:
        root = ET.fromstring(xml_bytes)
        for i, item in enumerate(root.iter("item")):
            title_el = item.find("title")
            if title_el is None or not title_el.text:
                continue
            title = title_el.text.strip()
 
            # The item's own <link> is just the generic feed URL, repeated on
            # every item. The real article link lives nested inside the
            # first <ht:news_item>/<ht:news_item_url>.
            article_url = None
            news_item = item.find("ht:news_item", GTRENDS_NS)
            if news_item is not None:
                url_el = news_item.find("ht:news_item_url", GTRENDS_NS)
                if url_el is not None and url_el.text:
                    article_url = url_el.text.strip()
            if not article_url:
                link_el = item.find("link")
                article_url = link_el.text.strip() if link_el is not None and link_el.text else "https://trends.google.com/trending"
 
            traffic_el = item.find("ht:approx_traffic", GTRENDS_NS)
            score = _parse_approx_traffic(traffic_el.text if traffic_el is not None else None)
            if score == 0:
                # fall back to feed order if traffic figure is missing/unparseable
                score = max(0, 100 - i * 5)
 
            items.append({
                "id": "gtrends:" + title.lower(),
                "title": title,
                "url": article_url,
                "score": score,
                "source": "Google Trends",
            })
    except ET.ParseError as e:
        log(f"google trends parse failed: {e}")
    return items
 
 
# ---------------------------------------------------------------------------
# State (avoid re-posting the same story)
# ---------------------------------------------------------------------------
 
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"state load failed, starting fresh: {e}")
        return {}
 
 
def save_state(state):
    # prune old entries so the file doesn't grow forever
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_MAX_AGE_DAYS)
    pruned = {}
    for k, v in state.items():
        try:
            ts = datetime.fromisoformat(v)
        except Exception:
            continue
        if ts >= cutoff:
            pruned[k] = v
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2)
 
 
def pick_candidate(items, state):
    unseen = [i for i in items if i["id"] not in state]
    if not unseen:
        return None
    unseen.sort(key=lambda i: i["score"], reverse=True)
    return unseen[0]
 
 
# ---------------------------------------------------------------------------
# Post text generation
# ---------------------------------------------------------------------------
 
def generate_with_claude(item):
    prompt = (
        "You write short, neutral notes for a Telegram news channel about "
        "what's currently trending/viral online. Given the story below, write "
        "2-3 sentences: one summarizing what it is, and one adding brief, "
        "even-handed context (why it might be getting attention, or what to "
        "watch next). No hashtags, no strong opinions, no political "
        "point-scoring. Plain text only.\n\n"
        f"Story: {item['title']}\n"
        f"Source: {item['source']}\n"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        log(f"claude generation failed, falling back to template: {e}")
        return None
 
 
def build_post_text(item):
    body = None
    if ANTHROPIC_API_KEY and CLAUDE_MODEL:
        body = generate_with_claude(item)
 
    if not body:
        body = f"Trending now via {item['source']}."
 
    return f"📈 {item['title']}\n\n{body}\n\n🔗 {item['url']}"
 
 
# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
 
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. Exiting.")
        sys.exit(1)
 
    candidates = []
    candidates += fetch_reddit_popular()
    candidates += fetch_google_trends()
 
    if not candidates:
        log("No candidates fetched from any source this run. Exiting.")
        return
 
    state = load_state()
    pick = pick_candidate(candidates, state)
 
    if not pick:
        log("Nothing new to post this run (all candidates already posted).")
        return
 
    text = build_post_text(pick)
    log(f"Posting: {pick['title']!r} (source={pick['source']}, score={pick['score']})")
    send_telegram(text)
 
    state[pick["id"]] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log("Posted and state saved.")
 
 
if __name__ == "__main__":
    main()
