"""Google ADK Reddit Monitor Agent and Hourly Digest Synthesizer."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure parent project directory is importable before loading config & sibling services
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Import config first so Vertex AI & GOOGLE_API_USE_CLIENT_CERTIFICATE=false env vars are set
import config  # noqa: F401, E402
from config import Config, DEFAULT_SUBREDDITS, resolve_mcp_command  # noqa: E402

from google.adk.agents import Agent  # noqa: E402
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams  # noqa: E402
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset  # noqa: E402
from mcp import StdioServerParameters  # noqa: E402

from services.discord_notifier import discord_notifier  # noqa: E402
from services.reddit_fetcher import reddit_fetcher  # noqa: E402
from services.storage_service import storage_service  # noqa: E402

# Strictly read-only tool allowlist from eliasbiondo/reddit-mcp-server
READ_ONLY_REDDIT_MCP_TOOLS: list[str] = [
    "reddit_get_subreddit_posts",
    "reddit_get_post",
    "reddit_search_subreddit",
    "reddit_search",
]


def create_reddit_mcp_toolset() -> McpToolset:
    """Create a strictly read-only MCP toolset backed by eliasbiondo/reddit-mcp-server."""
    command, args = resolve_mcp_command()

    mcp_env: dict[str, str] | None = None
    client_id = (Config.REDDIT_CLIENT_ID or "").strip()
    client_secret = (Config.REDDIT_CLIENT_SECRET or "").strip()
    if client_id and client_secret:
        mcp_env = {
            "REDDIT_CLIENT_ID": client_id,
            "REDDIT_CLIENT_SECRET": client_secret,
            "REDDIT_USER_AGENT": Config.REDDIT_USER_AGENT,
        }

    server_params = StdioServerParameters(
        command=command,
        args=args,
        env=mcp_env,
    )
    connection_params = StdioConnectionParams(
        server_params=server_params,
        timeout=20.0,
    )
    return McpToolset(
        connection_params=connection_params,
        tool_filter=list(READ_ONLY_REDDIT_MCP_TOOLS),
    )


def fetch_monitored_reddit_updates(
    subreddits_csv: str = "",
    limit_per_subreddit: int = 6,
    include_seen: bool = False,
) -> dict:
    """Fetch new Reddit posts across Haren's monitored AI, ML, Cloud, and Coding subreddits.

    Uses the primary eliasbiondo/reddit-mcp-server MCP pipeline with automatic fallback
    to Python requests JSON endpoints. Strictly read-only.

    Args:
        subreddits_csv: Optional comma-separated subreddit names (e.g. "LocalLLaMA,GeminiAI").
            Leave empty to check all configured monitored subreddits (14 by default).
        limit_per_subreddit: Maximum newest posts to inspect per subreddit (1 to 25).
        include_seen: Set True to return recently fetched posts even if seen in earlier runs.

    Returns:
        Dictionary containing fetched posts, counts, source breakdown, and any fetch errors.
    """
    settings = storage_service.get_settings()
    if subreddits_csv and subreddits_csv.strip():
        target_subreddits = [
            s.strip().removeprefix("r/").removeprefix("/r/").strip("/")
            for s in subreddits_csv.split(",")
            if s.strip()
        ]
    else:
        target_subreddits = list(settings.get("subreddits") or DEFAULT_SUBREDDITS)

    clamped_limit = max(1, min(int(limit_per_subreddit or 6), 25))
    seen_ids: set[str] = set() if include_seen else storage_service.get_seen_post_ids()

    result = reddit_fetcher.fetch_multiple_subreddits(
        subreddits=target_subreddits,
        limit_per_sub=clamped_limit,
        seen_ids=seen_ids,
        settings=settings,
    )

    all_posts = result.get("all_posts", [])
    new_posts = result.get("new_posts", [])
    selected_posts = all_posts if include_seen else new_posts

    return {
        "status": "ok",
        "subreddits_checked": target_subreddits,
        "limit_per_subreddit": clamped_limit,
        "include_seen": include_seen,
        "total_fetched_count": len(all_posts),
        "new_unseen_count": len(new_posts),
        "returned_posts_count": len(selected_posts),
        "posts": selected_posts,
        "source_breakdown": result.get("source_breakdown", {}),
        "errors": result.get("errors", []),
    }


def notify_discord_channel(headline: str, digest_markdown: str) -> dict:
    """Send a formatted Markdown Reddit intelligence update to Haren's Discord channel.

    Args:
        headline: Concise title for the Discord digest notification.
        digest_markdown: Well-structured Markdown body with key takeaways and post links.

    Returns:
        Dictionary indicating whether Discord is configured and whether delivery succeeded.
    """
    settings = storage_service.get_settings()
    configured, config_reason = discord_notifier.is_configured(settings=settings)
    if not configured:
        return {
            "sent": False,
            "configured": False,
            "message": f"Discord notifier is not configured: {config_reason}",
        }

    clean_headline = (headline or "HarenSmartAgent Reddit Intelligence Update").strip()
    sent, status_message = discord_notifier.send_digest(
        title=clean_headline,
        summary_markdown=digest_markdown,
        stats=None,
        settings=settings,
    )
    return {
        "sent": sent,
        "configured": True,
        "message": status_message,
    }


def get_stored_run_history(limit: int = 10) -> dict:
    """Retrieve chronological hourly run summaries stored in Google Cloud Storage (haren-agent-storage).

    Args:
        limit: Maximum number of recent runs to return in chronological order (1 to 200).

    Returns:
        Dictionary with chronological run records and summary counts.
    """
    clamped_limit = max(1, min(int(limit or 10), 200))
    runs = storage_service.list_runs(order="asc", limit=clamped_limit)

    compact_runs = []
    for run in runs:
        compact_runs.append(
            {
                "run_id": run.get("run_id"),
                "timestamp": run.get("timestamp") or run.get("started_at"),
                "trigger": run.get("trigger"),
                "status": run.get("status"),
                "subreddits_count": run.get(
                    "subreddits_count", len(run.get("subreddits_checked", []))
                ),
                "total_posts_fetched": run.get("total_posts_fetched", 0),
                "new_posts_count": run.get("new_posts_count", 0),
                "discord_notified": run.get("discord_notified", False),
                "short_summary": run.get("short_summary", ""),
            }
        )

    return {
        "status": "ok",
        "bucket": Config.GCS_BUCKET_NAME,
        "returned_runs_count": len(compact_runs),
        "runs": compact_runs,
    }


_MONITORED_SUBREDDITS_LIST = ", ".join(f"r/{sub}" for sub in DEFAULT_SUBREDDITS)

ROOT_AGENT_INSTRUCTION = f"""You are HarenSmartAgent (`haren_reddit_monitor`), Haren's personal AI & Cloud intelligence analyst powered by Google ADK.

STRICT SAFETY & READ-ONLY POLICY:
- You MUST NEVER post, comment, reply, vote, message users, or mutate anything on Reddit under any circumstances.
- Operate strictly in read-only observation and synthesis mode.

MONITORED SUBREDDITS (14 core communities):
{_MONITORED_SUBREDDITS_LIST}

YOUR RESPONSIBILITIES:
1. Monitor and inspect new posts across the 14 AI, ML, Cloud, and Coding subreddits using `fetch_monitored_reddit_updates` or the read-only Reddit MCP tools (`reddit_get_subreddit_posts`, `reddit_get_post`, `reddit_search_subreddit`, `reddit_search`).
2. Filter out low-signal memes, repetitive rants, or generic self-promotion. Highlight genuinely high-signal developments:
   - New frontier or open-weight model releases, benchmarks, and fine-tuning breakthroughs
   - Research papers, architectures, inference optimization, and hardware acceleration
   - AI agent frameworks, MCP tooling, coding workflows, and developer productivity tips
   - Google Cloud, Vertex AI, Gemini, Claude, LocalLLaMA, and Stable Diffusion updates
3. Structure your takeaways clearly by theme or topic, always tagging each item with `r/<subreddit>` and including clickable Markdown links (`[Post Title](permalink)`).
4. Use `get_stored_run_history` when asked about past hourly checks or trends stored in `gs://{Config.GCS_BUCKET_NAME}`.
5. Use `notify_discord_channel` when asked to push a digest or alert to Haren's Discord channel.
"""

DIGEST_SYNTHESIZER_INSTRUCTION = """You are Haren's AI/ML/Cloud Technical Digest Synthesizer (`haren_digest_synthesizer`).
You receive a batch of newly fetched Reddit posts across Haren's monitored AI, ML, Google Cloud, and AI Coding communities.

STRICT RULES:
- Rely ONLY on the provided batch of Reddit posts; never fabricate links, scores, or claims.
- Filter out low-effort noise, memes, or vague complaints, prioritizing substantive technical articles, model releases, papers, benchmarks, agent engineering workflows, cloud updates, and insightful engineering discussions.
- Even if a batch is modest in signal, extract the sharpest, most informative items truthfully.
- Every highlighted post MUST include its `r/<subreddit>` tag and a clickable Markdown link `[Post Title](permalink)`.

REQUIRED OUTPUT FORMAT:
Return your response using EXACTLY these two section headers in this order (do not wrap the entire output in a markdown code fence):

EXECUTIVE_SUMMARY:
Write 2-3 crisp, information-dense sentences summarizing the single most important developments and themes in this run. Keep it plain, executive-ready, and suitable for the chronological admin run table.

HOURLY_DIGEST:
Write a clean, scannable Markdown digest suitable for Discord and Haren's Admin Dashboard:
- Group notable items under 2-4 concise thematic headings (e.g., `### 🚀 Models & Research`, `### 🛠️ AI Coding & Agents`, `### ☁️ Cloud & Infrastructure`, `### 💬 Notable Community Discussions` — include only sections that have relevant posts).
- For each highlighted item, format as a bullet with **`[r/<subreddit>]` [Post Title](permalink)** followed by a crisp 1-2 sentence synthesis of why it matters (mention external article/repo links or engagement if noteworthy).
- Close with a brief 1-line `**Signal Check:**` noting total new posts reviewed and top active subreddits.
"""


def build_digest_synthesizer_agent() -> Agent:
    """Build a fast, tool-free ADK Agent that synthesizes fetched Reddit posts into a structured digest."""
    return Agent(
        name="haren_digest_synthesizer",
        model=Config.GEMINI_MODEL,
        description=(
            "Fast, tool-free synthesis agent that distills newly fetched Reddit posts into "
            "an executive summary and structured Markdown digest."
        ),
        instruction=DIGEST_SYNTHESIZER_INSTRUCTION,
        tools=[],
    )


root_agent = Agent(
    name="haren_reddit_monitor",
    model=Config.GEMINI_MODEL,
    description=(
        "Read-only Reddit intelligence agent monitoring 14 AI, ML, Cloud, and Coding "
        "subreddits via MCP + Python fallback, storing history in GCS, and notifying Discord."
    ),
    instruction=ROOT_AGENT_INSTRUCTION,
    tools=[
        create_reddit_mcp_toolset(),
        fetch_monitored_reddit_updates,
        notify_discord_channel,
        get_stored_run_history,
    ],
)
