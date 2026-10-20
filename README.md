# DSR Slack Bot

An AI-powered Slack bot that collects your Daily Status Report (DSR) via a natural conversation and automatically submits the time entries to your office portal.

## How It Works

```
You (Slack) → Bot (Gemini AI) → Portal API
```

1. You send `fill my dsr` on Slack
2. The bot (powered by Gemini) asks what you worked on
3. You describe your tasks in plain English — it figures out the category, project and time
4. Once 8 hours are confirmed, it auto-submits every entry to the portal
5. Done! ✅

---

## Setup

### Step 1: Slack App Configuration

In your Slack App ([api.slack.com](https://api.slack.com)):

1. **Enable Socket Mode**: Go to **Socket Mode** → Enable it → Create an App-Level Token with scope `connections:write` → Copy the token (starts with `xapp-`)
2. **Enable Events**: Go to **Event Subscriptions** → Enable Events → Subscribe to Bot Events: `message.im`, `message.channels`
3. **OAuth Scopes**: Go to **OAuth & Permissions** → Add Bot Token Scopes: `chat:write`, `im:history`, `channels:history`
4. **Install App**: Go to **Install App** → Install to Workspace → Copy the Bot Token (starts with `xoxb-`)

### Step 2: Create the `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
SLACK_SIGNING_SECRET=your-signing-secret
GEMINI_API_KEY=your-gemini-key
PORTAL_EMAIL=you@yourcompany.com
PORTAL_PASSWORD=your-portal-password
PORTAL_BASE_URL=https://portal-api.example.com
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

### Step 3: Run the Bot

```bash
bash start.sh
```

---

## File Structure

```
slackbot/
├── bot.py              # Main Slack bot (entry point)
├── ai_agent.py         # LLM conversation engine
├── prompts/            # LLM system prompts (markdown)
├── portal_service.py   # Portal API client
├── requirements.txt    # Python dependencies
├── start.sh            # One-command startup script
├── .env.example        # Environment variable template
└── .env                # Your credentials (DO NOT commit!)
```

---

## Example Conversation

```
You:  fill my dsr
Bot:  Hey there! Let's fill your DSR for today 👋 What did you work on?

You:  coding on my project
Bot:  Got it! 💻 Coding on Example Project. How long did you spend on this?

You:  4 hours
Bot:  ✅ Logged 4h for Coding. You still have 4 more hours to log. What else did you work on?

You:  scrum meeting and some testing
Bot:  Let's break that down:
      1. Scrum – how long?
      2. Testing – how long?

You:  30 min scrum, 3.5h testing
Bot:  ✅ Here's your summary so far:
      • Coding – Example Project (4h)
      • Scrum (30m)
      • Testing (3h 30m)
      Total: 8h ✅ Shall I submit this?

You:  yes
Bot:  🎉 DSR submitted! All 3 entries logged on the portal. Have a great evening! 🙌
```
