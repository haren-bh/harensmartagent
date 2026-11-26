"""Discord notification service supporting Webhooks, Bot Channels, and Bot Direct Messages."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

try:
    from config import Config
    from services.storage_service import storage_service
except ImportError:  # pragma: no cover - package-relative import fallback
    from ..config import Config  # type: ignore[no-redef]
    from .storage_service import storage_service  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_DISCORD_CHUNK_LEN = 1900


def _append_wait_query(webhook_url: str) -> str:
    """Ensure ?wait=true is present on the Discord webhook URL."""
    parsed = urlparse(webhook_url.strip())
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_params["wait"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query_params)))


def split_discord_chunks(text: str, max_len: int = MAX_DISCORD_CHUNK_LEN) -> list[str]:
    """Split markdown text cleanly into chunks <= max_len chars on newline boundaries."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_len:
        return [cleaned]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current_lines, current_len
        if current_lines:
            joined = "\n".join(current_lines).strip()
            if joined:
                chunks.append(joined)
            current_lines = []
            current_len = 0

    for line in cleaned.split("\n"):
        if len(line) <= max_len:
            added_len = len(line) if not current_lines else len(line) + 1
            if current_len + added_len <= max_len:
                current_lines.append(line)
                current_len += added_len
            else:
                _flush()
                current_lines.append(line)
                current_len = len(line)
        else:
            # Flush accumulated lines before splitting an oversized single line
            _flush()
            words = line.split(" ")
            segment = ""
            for word in words:
                if len(word) > max_len:
                    if segment:
                        chunks.append(segment)
                        segment = ""
                    for idx in range(0, len(word), max_len):
                        chunks.append(word[idx : idx + max_len])
                else:
                    candidate = f"{segment} {word}".strip() if segment else word
                    if len(candidate) <= max_len:
                        segment = candidate
                    else:
                        if segment:
                            chunks.append(segment)
                        segment = word
            if segment:
                current_lines.append(segment)
                current_len = len(segment)

    _flush()
    return chunks


class DiscordNotifier:
    """Delivers Reddit intelligence digests to Discord via Webhook or Bot API."""

    def _resolve_credentials(self, settings: dict[str, Any] | None = None) -> dict[str, str]:
        if settings is None:
            try:
                effective = storage_service.get_settings()
            except Exception:
                effective = {}
        else:
            effective = settings

        def _pick(key: str, fallback: str) -> str:
            if key in effective and effective[key] is not None:
                return str(effective[key]).strip()
            return str(fallback or "").strip()

        return {
            "webhook_url": _pick("discord_webhook_url", Config.DISCORD_WEBHOOK_URL),
            "bot_token": _pick("discord_bot_token", Config.DISCORD_BOT_TOKEN),
            "channel_id": _pick("discord_channel_id", Config.DISCORD_CHANNEL_ID),
            "user_id": _pick("discord_user_id", Config.DISCORD_USER_ID),
        }

    def is_configured(self, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Return (True, mode) where mode is 'webhook' | 'bot_channel' | 'bot_dm', or (False, 'not_configured')."""
        creds = self._resolve_credentials(settings)
        webhook_url = creds["webhook_url"]
        bot_token = creds["bot_token"]
        channel_id = creds["channel_id"]
        user_id = creds["user_id"]

        if webhook_url and webhook_url.startswith(("http://", "https://")):
            return True, "webhook"
        if bot_token and channel_id:
            return True, "bot_channel"
        if bot_token and user_id:
            return True, "bot_dm"
        return False, "not_configured"

    @staticmethod
    def _post_with_rate_limit_retry(
        session: requests.Session,
        url: str,
        json_payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        resp = session.post(url, json=json_payload, headers=headers, timeout=15)
        if resp.status_code == 429:
            retry_after = 1.5
            try:
                retry_after = min(float(resp.json().get("retry_after", 1.5)), 10.0)
            except Exception:
                pass
            time.sleep(retry_after)
            resp = session.post(url, json=json_payload, headers=headers, timeout=15)
        return resp

    @staticmethod
    def _format_digest_body(
        title: str, summary_markdown: str, stats: dict[str, Any] | None = None
    ) -> str:
        sections: list[str] = []
        clean_title = (title or "").strip()
        clean_body = (summary_markdown or "").strip()

        if clean_title and not clean_body.startswith(f"# {clean_title}") and not clean_body.startswith(f"## {clean_title}"):
            sections.append(f"**{clean_title}**")

        if isinstance(stats, dict) and stats:
            stat_parts: list[str] = []
            if "subreddits_checked" in stats:
                subs_val = stats["subreddits_checked"]
                sub_count = len(subs_val) if isinstance(subs_val, (list, tuple, set)) else subs_val
                stat_parts.append(f"Subreddits: `{sub_count}`")
            if "total_posts_fetched" in stats:
                stat_parts.append(f"Fetched: `{stats['total_posts_fetched']}`")
            if "new_posts_count" in stats:
                stat_parts.append(f"New: `{stats['new_posts_count']}`")
            if "trigger" in stats and stats["trigger"]:
                stat_parts.append(f"Trigger: `{stats['trigger']}`")
            if stat_parts:
                sections.append(" | ".join(stat_parts))

        if clean_body:
            sections.append(clean_body)

        return "\n\n".join(sections).strip()

    def _send_via_webhook(self, webhook_url: str, chunks: list[str]) -> tuple[bool, str]:
        target_url = _append_wait_query(webhook_url)
        with requests.Session() as sess:
            for idx, chunk in enumerate(chunks, start=1):
                payload = {
                    "content": chunk,
                    "username": "HarenSmartAgent",
                    "allowed_mentions": {"parse": []},
                }
                resp = self._post_with_rate_limit_retry(sess, target_url, payload)
                if not (200 <= resp.status_code < 300):
                    detail = (resp.text or "")[:250]
                    return (
                        False,
                        f"Webhook failed on chunk {idx}/{len(chunks)} (HTTP {resp.status_code}): {detail}",
                    )
        return True, f"Delivered {len(chunks)} chunk(s) via webhook"

    def _send_via_bot(
        self,
        bot_token: str,
        chunks: list[str],
        channel_id: str = "",
        user_id: str = "",
    ) -> tuple[bool, str]:
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/bharen/harensmartagent, 1.0)",
        }

        with requests.Session() as sess:
            resolved_channel_id = channel_id.strip()
            mode_label = "bot_channel"

            if not resolved_channel_id:
                mode_label = "bot_dm"
                dm_resp = self._post_with_rate_limit_retry(
                    sess,
                    f"{DISCORD_API_BASE}/users/@me/channels",
                    {"recipient_id": user_id.strip()},
                    headers=headers,
                )
                if not (200 <= dm_resp.status_code < 300):
                    detail = (dm_resp.text or "")[:250]
                    return (
                        False,
                        f"Failed creating Discord DM channel (HTTP {dm_resp.status_code}): {detail}",
                    )
                try:
                    resolved_channel_id = str(dm_resp.json().get("id") or "").strip()
                except ValueError:
                    resolved_channel_id = ""
                if not resolved_channel_id:
                    return False, "Discord DM channel response did not include a channel id."

            msg_url = f"{DISCORD_API_BASE}/channels/{resolved_channel_id}/messages"
            for idx, chunk in enumerate(chunks, start=1):
                payload = {
                    "content": chunk,
                    "allowed_mentions": {"parse": []},
                }
                resp = self._post_with_rate_limit_retry(
                    sess,
                    msg_url,
                    payload,
                    headers=headers,
                )
                if not (200 <= resp.status_code < 300):
                    detail = (resp.text or "")[:250]
                    return (
                        False,
                        f"Discord Bot API failed on chunk {idx}/{len(chunks)} (HTTP {resp.status_code}): {detail}",
                    )

        return True, f"Delivered {len(chunks)} chunk(s) via {mode_label}"

    def send_digest(
        self,
        title: str,
        summary_markdown: str,
        stats: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Format and send a Reddit digest to Discord, chunking safely <= 1900 chars."""
        configured, mode = self.is_configured(settings)
        if not configured:
            return False, "not_configured"

        creds = self._resolve_credentials(settings)
        full_message = self._format_digest_body(title, summary_markdown, stats=stats)
        chunks = split_discord_chunks(full_message, max_len=MAX_DISCORD_CHUNK_LEN)
        if not chunks:
            return False, "Digest message content was empty."

        try:
            if mode == "webhook":
                return self._send_via_webhook(creds["webhook_url"], chunks)
            if mode == "bot_channel":
                return self._send_via_bot(
                    creds["bot_token"],
                    chunks,
                    channel_id=creds["channel_id"],
                )
            if mode == "bot_dm":
                return self._send_via_bot(
                    creds["bot_token"],
                    chunks,
                    user_id=creds["user_id"],
                )
            return False, f"Unsupported Discord mode: {mode}"
        except requests.RequestException as exc:
            logger.warning("Discord notification request error (%s): %s", mode, exc)
            return False, f"Discord network error ({mode}): {exc}"
        except Exception as exc:
            logger.exception("Unexpected error sending Discord digest")
            return False, f"Discord unexpected error ({mode}): {exc}"

    def send_test_message(
        self, settings: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        """Send a test notification to verify Discord Webhook or Bot configuration."""
        configured, mode = self.is_configured(settings)
        if not configured:
            return (
                False,
                "Discord is not configured. Configure DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID/DISCORD_USER_ID.",
            )

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = (
            f"Connectivity check succeeded using delivery mode `{mode}` at `{now_iso}`.\n"
            "HarenSmartAgent (Reddit Monitor ADK Agent) is ready to deliver hourly digests."
        )
        return self.send_digest(
            title="HarenSmartAgent — Discord Test Notification",
            summary_markdown=body,
            stats=None,
            settings=settings,
        )


discord_notifier = DiscordNotifier()
