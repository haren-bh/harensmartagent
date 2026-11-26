# HarenSmartAgent — Reddit AI/Cloud Monitor (Google ADK + Flask Admin)

An autonomous **Google ADK (Agent Development Kit)** monitoring agent that tracks **14 AI, ML, Cloud, and Coding subreddits** for new articles every hour, synthesizes executive digests with Gemini, sends updates to **Discord**, persists all run history to **Google Cloud Storage (`gs://haren-agent-storage`)**, and exposes a private **gray-and-white Flask Admin Dashboard**.

## Monitored Subreddits

1. `r/singularity` (`https://www.reddit.com/r/singularity/`)
2. `r/ArtificialInteligence` (`https://www.reddit.com/r/ArtificialInteligence/`)
3. `r/MachineLearning` (`https://www.reddit.com/r/MachineLearning/`)
4. `r/accelerate` (`https://www.reddit.com/r/accelerate/`)
5. `r/GeminiAI` (`https://www.reddit.com/r/GeminiAI/`)
6. `r/googlecloud` (`https://www.reddit.com/r/googlecloud/`)
7. `r/AI_Agents` (`https://www.reddit.com/r/AI_Agents/`)
8. `r/LocalLLaMA` (`https://www.reddit.com/r/LocalLLaMA/`)
9. `r/ChatGPTCoding` (`https://www.reddit.com/r/ChatGPTCoding/`)
10. `r/ClaudeAI` (`https://www.reddit.com/r/ClaudeAI/`)
11. `r/LLMDevs` (`https://www.reddit.com/r/LLMDevs/`)
12. `r/deeplearning` (`https://www.reddit.com/r/deeplearning/`)
13. `r/artificial` (`https://www.reddit.com/r/artificial/`)
14. `r/StableDiffusion` (`https://www.reddit.com/r/StableDiffusion/`)

> **Strict Read-Only Guarantee**: Neither the MCP toolset nor the Python backup fetcher ever creates posts, comments, edits, or votes on Reddit.

## Reddit Connectivity Architecture

1. **Primary Connector (`eliasbiondo/reddit-mcp-server`)**:
   - Uses `reddit-no-auth-mcp-server` via MCP `stdio` (`McpToolset` in ADK + MCP `ClientSession` for batch runs), restricted strictly to read-only tools (`reddit_get_subreddit_posts`, `reddit_get_post`, `reddit_search_subreddit`, `reddit_search`).
   - Supports optional `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` if anonymous Reddit requests are blocked by network security.
2. **Automatic Python Requests Backup**:
   - Automatically falls back to Python HTTP requests + BeautifulSoup (`requests` + `curl_cffi` with automatic Reddit JS challenge solver and optional read-only session cookies) whenever MCP returns HTTP 403 or errors.

## What Is Needed From Your Discord

Choose **either** Option A (fastest, ~30 seconds) or Option B:

### Option A — Discord Webhook URL (Recommended)
1. Open Discord and go to the server/channel where you want hourly updates.
2. Click the gear icon (**Edit Channel**) -> **Integrations** -> **Webhooks** -> **New Webhook**.
3. Copy the **Webhook URL** (`https://discord.com/api/webhooks/...`) and paste it into the **Admin Dashboard -> Discord Configuration** panel (or set `DISCORD_WEBHOOK_URL` in `.env`).

### Option B — Discord Bot (Channel Message or Direct Message)
1. Create an app at <https://discord.com/developers/applications>, go to **Bot**, click **Reset Token**, and copy your **Bot Token** (`DISCORD_BOT_TOKEN`).
2. Invite the bot to your server with `Send Messages` / `Embed Links` permissions.
3. Enable **Developer Mode** in Discord (**User Settings -> Advanced -> Developer Mode**), then:
   - For a **channel**: Right-click the channel -> **Copy Channel ID** (`DISCORD_CHANNEL_ID`), OR
   - For a **Direct Message (DM)** to you: Right-click your profile -> **Copy User ID** (`DISCORD_USER_ID`, requires sharing at least one server with the bot).

## Running With Your Python Environment

Start the Flask Admin Web App + Hourly Background Scheduler:

```bash
/Users/bharen/venv/mainvenv/bin/python3 app.py
```

- **Admin URL**: `http://127.0.0.1:5050`
- **Admin ID**: `haren_admin`
- **Admin Password**: `Haren@SmartAgent2026!` (configurable via `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env`)

Run a single monitoring pass from CLI:

```bash
/Users/bharen/venv/mainvenv/bin/python3 run_once.py
```

Run interactively in ADK Dev UI:

```bash
/Users/bharen/venv/mainvenv/bin/python3 -m google.adk.cli web .
```
