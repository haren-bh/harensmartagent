# HarenSmartAgent — Change Log & Technical Specifications (`specs.md`)

## 2026-11-26 — Initial Creation of HarenSmartAgent (Google ADK Reddit Monitor + Discord Notifier + GCS Storage + Gray/White Flask Admin UI)

### Summary of Changes
Created a complete Google ADK (Agent Development Kit) monitoring system and private Flask Admin Web Application in `/Users/bharen/Documents/Code/harensmartagent/harensmartagent` running on `/Users/bharen/venv/mainvenv/bin/python3`.

### Exact File-by-File Changes Made
1. **`config.py`, `.env.example`, `.env`, `requirements.txt`**:
   - Configured Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT=haren-main`, `GOOGLE_CLOUD_LOCATION=us-central1`) and `GOOGLE_API_USE_CLIENT_CERTIFICATE=false` (preventing macOS `aiohttp` mTLS transport errors during async ADK `Runner` calls).
   - Configured the 14 monitored subreddits (`singularity`, `ArtificialInteligence`, `MachineLearning`, `accelerate`, `GeminiAI`, `googlecloud`, `AI_Agents`, `LocalLLaMA`, `ChatGPTCoding`, `ClaudeAI`, `LLMDevs`, `deeplearning`, `artificial`, `StableDiffusion`).
   - Configured Google Cloud Storage bucket `haren-agent-storage` (`harensmartagent/` prefix), hourly schedule interval (`60` minutes), read-only Reddit settings, Discord notification settings, and Haren-specific admin credentials (`ADMIN_USERNAME=haren_admin`).
2. **`services/storage_service.py`**:
   - Implemented `StorageService` persisting runtime settings (`harensmartagent/config/runtime_settings.json`), deduplicated seen post IDs (`harensmartagent/state/seen_posts.json`, capped at 5,000 IDs), individual run records (`harensmartagent/runs/<timestamp>_<run_id>.json`), and run index (`harensmartagent/index/runs_index.json`) in `gs://haren-agent-storage` with local cache fallback in `instance/storage_cache`.
   - Implemented chronological sorting (`list_runs(order="asc")` oldest-to-newest default, plus `order="desc"` toggle).
3. **`services/reddit_fetcher.py`**:
   - Implemented strictly **read-only** `RedditFetcher` (zero post/comment/vote/write endpoints).
   - **Primary connector**: Connects to `eliasbiondo/reddit-mcp-server` (`reddit-no-auth-mcp-server`) over MCP `stdio` calling `reddit_get_subreddit_posts` (`category="new"`), forwarding optional `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`.
   - **Backup connector**: Automatically falls back on HTTP 403 / MCP error to Python HTTP requests + BeautifulSoup (`requests.Session` `.json` and shared `curl_cffi.requests.Session` with automatic Reddit JS challenge solver + `<shreddit-post>` HTML feed parser + optional read-only session login cookie fallback).
4. **`services/discord_notifier.py`**:
   - Implemented `DiscordNotifier` supporting both **Discord Webhook URL** (`DISCORD_WEBHOOK_URL`) and **Discord Bot REST API v10** (`DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` for server channel messages or `DISCORD_USER_ID` for Direct Messages), with newline-aware <=1,900 character chunking and test ping support.
5. **`reddit_monitor_agent/__init__.py` & `reddit_monitor_agent/agent.py`**:
   - Created the Google ADK `root_agent` (`haren_reddit_monitor`, `gemini-2.5-flash`) equipped with `McpToolset` (`eliasbiondo/reddit-mcp-server` filtered strictly to read-only tools: `reddit_get_subreddit_posts`, `reddit_get_post`, `reddit_search_subreddit`, `reddit_search`) and custom ADK tools (`fetch_monitored_reddit_updates`, `notify_discord_channel`, `get_stored_run_history`).
   - Created `build_digest_synthesizer_agent()` (`haren_digest_synthesizer`) producing structured `EXECUTIVE_SUMMARY:` and `HOURLY_DIGEST:` Markdown output.
6. **`services/runner_service.py` & `run_once.py`**:
   - Implemented `AgentRunnerService` orchestrating ADK `Runner` + `InMemorySessionService`, hourly `APScheduler` `BackgroundScheduler` jobs, seen-post deduplication, Discord delivery, and `gs://haren-agent-storage` persistence, plus CLI runner `run_once.py`.
7. **`app.py`, `templates/base.html`, `templates/login.html`, `templates/dashboard.html`, `static/styles.css`, `README.md`**:
   - Built the Haren-only Flask Admin Web Application in a crisp **gray and white** architectural theme (`#f4f5f7`, `#ffffff`, `#e5e7eb`, `#111827`, `#1f2937`).
   - Enforced session authentication (`hmac.compare_digest`), `HttpOnly`/`SameSite=Lax` cookies, CSRF protection on all `POST` endpoints, secret masking, manual "Run Now" trigger with live `/api/status` polling, hourly scheduler pause/resume/interval controls, Discord configuration + test sender, subreddit manager, and a `@container` responsive chronological run summaries table with native `<dialog>` full-digest viewer.
