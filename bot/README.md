# Kal — Bot Setup

## Morning Brief Setup (Gmail IMAP)

Kal reads multiple financial newsletters every morning and synthesizes them into
one "what's the trade?" brief posted to Discord at ~6:30am ET.

### Step 1 — Gmail App Password (one-time setup)

Kal uses `kalpredictslp@gmail.com` with an App Password for IMAP access.
This works 24/7 on Railway with no browser interaction.

**Enable 2-Step Verification** (required for App Passwords):
1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Security → 2-Step Verification → Turn on

**Generate the App Password:**
1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Select **Mail** and **Windows Computer**
3. Click **Generate**
4. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)
5. Add to `bot/.env`:
   ```
   KAL_EMAIL_ADDRESS=kalpredictslp@gmail.com
   KAL_EMAIL_PASSWORD=abcd efgh ijkl mnop
   ```

**Enable IMAP in Gmail:**
1. Open Gmail → Settings (gear icon) → See all settings
2. Forwarding and POP/IMAP tab → Enable IMAP → Save Changes

### Step 2 — Subscribe to newsletters

Subscribe `kalpredictslp@gmail.com` to all five newsletters:

| Newsletter | Sender | Subscribe |
|---|---|---|
| ExecSum | news@execsum.co | execsum.co |
| Crypto Briefing (Beehiiv) | cryptosum@mail.beehiiv.com | Find via sender |
| Daily Friday (Beehiiv) | dailyfriday@mail.beehiiv.com | Find via sender |
| Big Desk Energy | tyler@mail.bigdeskenergy.com | bigdeskenergy.com |
| The AI Report (Beehiiv) | theaireport@mail.beehiiv.com | Find via sender |

All five are configured in `NEWSLETTER_EMAILS` in `.env`. Kal fetches all of
them in a single IMAP connection each morning and synthesizes into one brief
with one Claude call.

### Step 3 — Push to Railway

Once you've set the App Password in `bot/.env`, run:
```bash
cd C:\Users\andre\Desktop\kalshi-bot
python bot/setup_railway.py
```

This pushes `KAL_EMAIL_ADDRESS`, `KAL_EMAIL_PASSWORD`, and `NEWSLETTER_EMAILS`
to Railway automatically.

### How it works

- Checks every 10 minutes from 6:00–8:00am ET (11:00–13:00 UTC)
- One IMAP connection opens, searches for today's emails from all 5 senders
- Any newsletters found are passed to Claude in one API call
- Claude synthesizes them into a unified brief with 7 sections
- Brief posted to `#morning-brief`, Today's Focus snippet to `#intelligence`
- Never posts twice the same day

---

## Gmail OAuth2 (local development only)

OAuth2 requires a browser and is NOT suitable for Railway. Use IMAP above instead.
Keep these instructions for reference if you need to run locally without App Password.

**Create a Google Cloud project:**
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project → Enable Gmail API → Create OAuth credentials (Desktop app)
3. Download `gmail_credentials.json` → place in `bot/`

**First-time authorization:**
```bash
cd bot
python -c "from email_reader import EmailReader; import asyncio; r = EmailReader('./gmail_credentials.json', './gmail_token.json'); asyncio.run(r.fetch_newsletter('test@example.com'))"
```

Set in `.env` (local only, not Railway):
```
GMAIL_CREDENTIALS_PATH=./gmail_credentials.json
GMAIL_TOKEN_PATH=./gmail_token.json
```

---

## Other API Keys

| Key | Where to get | What it enables |
|-----|-------------|-----------------|
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) (free) | Breaking news, economic calendar, market alerts |
| `ALPHA_VANTAGE_KEY` | [alphavantage.co](https://www.alphavantage.co) (free, 25 calls/day) | SPY, GLD, USO, QQQ quotes in briefings |

Both are optional — Kal runs without them, those features stay silent.
