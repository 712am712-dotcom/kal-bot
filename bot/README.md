# Kal — Bot Setup

## Gmail API Setup (for Morning Brief)

Kal reads your morning financial newsletter from Gmail and posts a condensed
"what's the trade?" brief to Discord every morning at 6:30am ET.

### Step-by-step

**1. Create a Google Cloud project**
- Go to [console.cloud.google.com](https://console.cloud.google.com)
- Click "Select a project" → "New Project"
- Name it (e.g. "Kal Bot") → Create

**2. Enable the Gmail API**
- In your new project, go to "APIs & Services" → "Library"
- Search for "Gmail API" → Click it → "Enable"

**3. Configure the OAuth consent screen**
- Go to "APIs & Services" → "OAuth consent screen"
- Select "External" → Create
- Fill in App name (e.g. "Kal"), your email for support and developer contact
- Click "Save and Continue" through all steps
- On the "Test users" step, add your Gmail address → Save

**4. Create OAuth2 credentials**
- Go to "APIs & Services" → "Credentials"
- Click "Create Credentials" → "OAuth client ID"
- Application type: **Desktop app**
- Name: "Kal Bot"
- Click "Create"

**5. Download credentials**
- Click the download icon (⬇) next to your new credential
- Save the file as `gmail_credentials.json`
- Place it in the `bot/` folder

**6. Set your newsletter sender in .env**
```
NEWSLETTER_EMAIL=newsletter@example.com
GMAIL_CREDENTIALS_PATH=./gmail_credentials.json
GMAIL_TOKEN_PATH=./gmail_token.json
```

**7. First-time authorization (run once)**
```bash
cd bot
python -c "from gmail_reader import GmailReader; import asyncio; r = GmailReader('./gmail_credentials.json', './gmail_token.json'); asyncio.run(r.fetch_newsletter('test@example.com'))"
```
A browser window will open asking you to authorize Kal to read your Gmail.
Click "Allow" — this is read-only (no send/delete access).

The token is saved to `gmail_token.json` automatically.

**8. Done** — Kal will check for your newsletter at 6:15am ET every morning.
If it arrives, it posts the brief to `#morning-brief` at 6:30am ET.

---

### Security notes
- `gmail_credentials.json` and `gmail_token.json` are in `.gitignore` — never committed
- OAuth scope is `gmail.readonly` — Kal can never send, delete, or modify emails
- Kal only searches for emails from `NEWSLETTER_EMAIL` — no other emails accessed

---

## Other API Keys

| Key | Where to get | What it enables |
|-----|-------------|-----------------|
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) (free) | Breaking news, market news, economic calendar, news-to-Kalshi thesis builder |
| `ALPHA_VANTAGE_KEY` | [alphavantage.co](https://www.alphavantage.co) (free, 25 calls/day) | SPY, GLD, USO, QQQ, SLV quotes in daily/weekly briefings |

Both are optional — Kal runs without them, those features just stay silent.
