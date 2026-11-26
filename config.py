"""Central configuration for HarenSmartAgent (Reddit Monitor ADK Agent & Admin Web App)."""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Prevent aiohttp mTLS endpoint issues on macOS when using Application Default Credentials
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

# Default to Vertex AI using active GCP project unless an explicit Gemini API key is configured
if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "haren-main")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

DEFAULT_SUBREDDITS: list[str] = [
    "singularity",
    "ArtificialInteligence",
    "MachineLearning",
    "accelerate",
    "GeminiAI",
    "googlecloud",
    "AI_Agents",
    "LocalLLaMA",
    "ChatGPTCoding",
    "ClaudeAI",
    "LLMDevs",
    "deeplearning",
    "artificial",
    "StableDiffusion",
]


def resolve_mcp_command() -> tuple[str, list[str]]:
    """Resolve the eliasbiondo/reddit-mcp-server executable command and arguments."""
    explicit_bin = os.environ.get("REDDIT_MCP_BIN", "").strip()
    if explicit_bin and Path(explicit_bin).exists():
        return explicit_bin, []

    venv_bin = Path("/Users/bharen/venv/mainvenv/bin/reddit-no-auth-mcp-server")
    if venv_bin.exists():
        return str(venv_bin), []

    which_bin = shutil.which("reddit-no-auth-mcp-server")
    if which_bin:
        return which_bin, []

    uvx_bin = shutil.which("uvx") or "/Library/Frameworks/Python.framework/Versions/3.12/bin/uvx"
    return uvx_bin, ["reddit-no-auth-mcp-server"]


class Config:
    """Application & ADK Agent settings."""

    # ADK / Gemini model
    ADK_APP_NAME: str = os.environ.get("ADK_APP_NAME", "haren_reddit_monitor")
    GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # Google Cloud Storage
    GCS_BUCKET_NAME: str = os.environ.get("GCS_BUCKET_NAME", "haren-agent-storage")
    GCS_PREFIX: str = os.environ.get("GCS_PREFIX", "harensmartagent")
    LOCAL_FALLBACK_DIR: Path = BASE_DIR / "instance" / "storage_cache"

    # Reddit settings (STRICTLY READ-ONLY — never post, comment, or vote)
    REDDIT_USERNAME: str = os.environ.get("REDDIT_USERNAME", "")
    REDDIT_PASSWORD: str = os.environ.get("REDDIT_PASSWORD", "")
    REDDIT_CLIENT_ID: str = os.environ.get("REDDIT_CLIENT_ID", "")
    REDDIT_CLIENT_SECRET: str = os.environ.get("REDDIT_CLIENT_SECRET", "")
    REDDIT_USER_AGENT: str = os.environ.get(
        "REDDIT_USER_AGENT",
        "macos:com.haren.smartagent:v1.0.0 (by /u/badtemperedpeanut)",
    )
    POSTS_PER_SUBREDDIT: int = int(os.environ.get("POSTS_PER_SUBREDDIT", "8"))

    # Discord notification settings (can also be updated via Admin Web UI into GCS settings)
    DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
    DISCORD_BOT_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    DISCORD_CHANNEL_ID: str = os.environ.get("DISCORD_CHANNEL_ID", "")
    DISCORD_USER_ID: str = os.environ.get("DISCORD_USER_ID", "")

    # Scheduler settings
    SCHEDULE_INTERVAL_MINUTES: int = int(os.environ.get("SCHEDULE_INTERVAL_MINUTES", "60"))
    SCHEDULER_AUTO_START: bool = os.environ.get("SCHEDULER_AUTO_START", "true").lower() in (
        "1",
        "true",
        "yes",
    )

    # Flask Admin Web UI settings (viewable by Haren only)
    ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "haren_admin")
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "Haren@SmartAgent2026!")
    FLASK_SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    FLASK_HOST: str = os.environ.get("FLASK_HOST", "127.0.0.1")
    FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "5050"))
