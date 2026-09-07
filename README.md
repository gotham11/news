# Viral News Bot

Checks Reddit r/popular and Google Trends once per run, picks the top story
it hasn't posted yet, writes a short neutral note about it (via Claude, if
configured), and posts it to your Telegram channel.

## Your bot & channel (already set up)

- Bot: `@cnewsbbot`
- Channel: `@cnews5`

## Setup (GitHub Actions — free, always-on, no server needed)

1. Create a **new GitHub repository** (private is fine — it'll also store a
   small `posted.json` history file) and push these files to it.

2. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret** and add:

   | Secret name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | `8676837922:AAGQ2xeOW-yWmjsAKftV7NysPCw-VlqocvY` |
   | `TELEGRAM_CHAT_ID` | `@cnews5` |
   | `ANTHROPIC_API_KEY` *(optional)* | your Anthropic API key, if you want Claude to write the commentary |
   | `CLAUDE_MODEL` *(optional, required if you set the key above)* | a current model id — check https://docs.claude.com/en/docs/about-claude/models |

   > That bot token is a credential — anyone with it can control `@cnewsbbot`.
   > Only put it in the repo's encrypted Secrets, never in a committed file.

3. Push to the repo's default branch. GitHub Actions will pick up
   `.github/workflows/viral-news-bot.yml` automatically and start running it
   **every hour**.

4. To test it right away instead of waiting for the next hour: go to the
   **Actions** tab → "Viral News Bot" → **Run workflow**.

If `ANTHROPIC_API_KEY`/`CLAUDE_MODEL` aren't set, the bot still runs — it
just posts a plain "trending now" line instead of an AI-written note.

## Running it somewhere else instead

It's a plain script, so a VPS/cron works too:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="8676837922:AAGQ2xeOW-yWmjsAKftV7NysPCw-VlqocvY"
export TELEGRAM_CHAT_ID="@cnews5"
# optional:
# export ANTHROPIC_API_KEY="..."
# export CLAUDE_MODEL="..."
python bot.py
```

Add that to cron (e.g. `0 * * * *`) for hourly runs. `posted.json` (created
next to the script) is what stops it from posting the same story twice —
keep that file around between runs.

## Notes / next steps

- "Viral" here means top of r/popular (last hour) + Google Trends daily
  trends for the US. Easy to extend with more sources later (Twitter/X
  trends need a paid API, so it's not included yet).
- Checks are hourly, not real-time — a story has to already be trending
  when the check runs.
- The bot posts at most one story per run; if several qualify, it always
  picks the highest-scoring one it hasn't posted before.
