"""ADK Runner execution service and APScheduler background hourly scheduler."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# Ensure Vertex AI & certificate env vars are configured before ADK/GenAI imports
import config  # noqa: F401
from config import Config, DEFAULT_SUBREDDITS

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from reddit_monitor_agent.agent import build_digest_synthesizer_agent
from services.discord_notifier import discord_notifier
from services.reddit_fetcher import reddit_fetcher
from services.storage_service import storage_service

logger = logging.getLogger(__name__)

MAX_POSTS_IN_PROMPT = 75
MAX_SELFTEXT_CHARS = 480


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _extract_post_id(post: dict[str, Any]) -> str:
    """Extract canonical post ID string from a fetched post record."""
    raw_id = str(post.get("id") or post.get("post_id") or post.get("name") or "").strip()
    if raw_id.startswith("t3_"):
        return raw_id[3:]
    return raw_id


def _normalize_permalink(post: dict[str, Any]) -> str:
    """Return full https://www.reddit.com permalink for a post."""
    permalink = str(post.get("permalink") or "").strip()
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    url = str(post.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    subreddit = str(post.get("subreddit") or "all").strip().removeprefix("r/")
    post_id = _extract_post_id(post)
    if post_id:
        return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    return f"https://www.reddit.com/r/{subreddit}/"


def _compact_article_record(post: dict[str, Any]) -> dict[str, Any]:
    """Return a clean, compact dictionary of post metadata for GCS run storage."""
    return {
        "id": _extract_post_id(post),
        "subreddit": str(post.get("subreddit") or "").removeprefix("r/"),
        "title": str(post.get("title") or "(Untitled)").strip(),
        "permalink": _normalize_permalink(post),
        "url": str(post.get("url") or post.get("external_url") or _normalize_permalink(post)),
        "author": str(post.get("author") or "unknown"),
        "score": int(post.get("score") or post.get("ups") or 0),
        "num_comments": int(post.get("num_comments") or post.get("comments_count") or 0),
        "created_utc": post.get("created_utc") or post.get("created_iso"),
        "source": str(post.get("source") or "unknown"),
    }


def _build_synthesis_prompt(
    posts: list[dict[str, Any]],
    subreddits: list[str],
    total_fetched: int,
    ignore_seen: bool,
) -> str:
    """Format fetched Reddit posts into a structured prompt for `haren_digest_synthesizer`."""
    # Prioritize higher-engagement or newest posts while capping prompt size cleanly
    sorted_posts = sorted(
        posts,
        key=lambda p: (
            int(p.get("score") or p.get("ups") or 0)
            + int(p.get("num_comments") or 0) * 2,
            float(p.get("created_utc") or 0)
            if isinstance(p.get("created_utc"), (int, float))
            else 0.0,
        ),
        reverse=True,
    )
    selected = sorted_posts[:MAX_POSTS_IN_PROMPT]

    lines: list[str] = [
        f"Run Timestamp (UTC): {_utc_now_iso()}",
        f"Monitored Subreddits Checked ({len(subreddits)}): {', '.join('r/' + s for s in subreddits)}",
        f"Total Recent Posts Scanned: {total_fetched}",
        f"Candidate Posts in Batch: {len(posts)} (showing top {len(selected)} below; ignore_seen={ignore_seen})",
        "",
        "POSTS BATCH:",
    ]

    for idx, post in enumerate(selected, start=1):
        subreddit = str(post.get("subreddit") or "unknown").removeprefix("r/")
        title = str(post.get("title") or "(Untitled)").replace("\n", " ").strip()
        permalink = _normalize_permalink(post)
        external_url = str(post.get("url") or post.get("external_url") or "").strip()
        score = int(post.get("score") or post.get("ups") or 0)
        num_comments = int(post.get("num_comments") or post.get("comments_count") or 0)
        author = str(post.get("author") or "unknown")
        flair = str(post.get("flair") or post.get("link_flair_text") or "").strip()
        body = str(post.get("selftext") or post.get("body") or post.get("summary") or "").strip()
        if len(body) > MAX_SELFTEXT_CHARS:
            body = body[:MAX_SELFTEXT_CHARS].rstrip() + "..."

        entry = [
            f"{idx}. [r/{subreddit}] {title}",
            f"   - Permalink: {permalink}",
            f"   - Score: {score} | Comments: {num_comments} | Author: u/{author}"
            + (f" | Flair: {flair}" if flair else ""),
        ]
        if external_url and external_url != permalink:
            entry.append(f"   - Linked URL: {external_url}")
        if body:
            compact_body = " ".join(body.split())
            entry.append(f"   - Excerpt: {compact_body}")
        lines.extend(entry)

    return "\n".join(lines)


def _parse_synthesizer_output(raw_text: str, fallback_count: int, subreddits_count: int) -> tuple[str, str]:
    """Extract (short_summary, hourly_digest_markdown) from synthesizer agent output."""
    cleaned = (raw_text or "").strip()
    if not cleaned:
        fallback_short = (
            f"Reviewed {fallback_count} new posts across {subreddits_count} subreddits."
        )
        return fallback_short, fallback_short

    exec_match = re.search(
        r"EXECUTIVE_SUMMARY\s*:\s*(.*?)(?=\n\s*#{0,3}\s*\*?\*?HOURLY_DIGEST\s*:|\Z)",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    digest_match = re.search(
        r"HOURLY_DIGEST\s*:\s*(.*)\Z",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    short_summary = ""
    full_digest = ""

    if exec_match:
        short_summary = exec_match.group(1).strip().strip("*").strip()
        # Collapse excessive newlines for compact chronological table display
        short_summary = re.sub(r"\s*\n+\s*", " ", short_summary).strip()

    if digest_match:
        full_digest = digest_match.group(1).strip()

    if not full_digest:
        full_digest = cleaned

    if not short_summary:
        # Derive 2-sentence plain summary from first non-heading lines
        plain_lines = [
            ln.strip("-*# ").strip()
            for ln in full_digest.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        joined = " ".join(plain_lines)
        short_summary = joined[:360].rsplit(" ", 1)[0] + "..." if len(joined) > 360 else joined
        if not short_summary:
            short_summary = (
                f"Synthesized {fallback_count} new posts across {subreddits_count} subreddits."
            )

    return short_summary, full_digest


def _build_fallback_digest(posts: list[dict[str, Any]], subreddits: list[str]) -> tuple[str, str]:
    """Construct a deterministic Markdown digest if the LLM call encounters a transient error."""
    top_posts = sorted(
        posts,
        key=lambda p: int(p.get("score") or p.get("ups") or 0),
        reverse=True,
    )[:12]
    active_subs = sorted({str(p.get("subreddit") or "").removeprefix("r/") for p in posts if p.get("subreddit")})
    short_summary = (
        f"Fetched {len(posts)} new articles across {len(active_subs)} active subreddits "
        f"({', '.join('r/' + s for s in active_subs[:5])})."
    )
    md_lines = [
        "### 📰 Newly Fetched Reddit Articles",
        "",
    ]
    for post in top_posts:
        sub = str(post.get("subreddit") or "unknown").removeprefix("r/")
        title = str(post.get("title") or "(Untitled)").replace("[", "(").replace("]", ")").strip()
        permalink = _normalize_permalink(post)
        score = int(post.get("score") or post.get("ups") or 0)
        comments = int(post.get("num_comments") or post.get("comments_count") or 0)
        md_lines.append(
            f"- **`[r/{sub}]` [{title}]({permalink})** — {score} pts · {comments} comments"
        )
    md_lines.append("")
    md_lines.append(
        f"**Signal Check:** {len(posts)} new posts across {len(subreddits)} monitored subreddits."
    )
    return short_summary, "\n".join(md_lines)


class AgentRunnerService:
    """Executes the Google ADK digest synthesizer and manages the APScheduler hourly job."""

    JOB_ID = "haren_hourly_reddit_monitor"

    def __init__(self) -> None:
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._scheduler: BackgroundScheduler | None = None
        self._scheduler_initialized: bool = False

        self._is_running: bool = False
        self._current_run_started_at: str | None = None
        self._interval_minutes: int = Config.SCHEDULE_INTERVAL_MINUTES
        self._last_run_at: str | None = None
        self._last_run_status: str | None = None
        self._history_hydrated: bool = False

    def _hydrate_last_run_from_storage(self) -> None:
        """Populate `_last_run_at` and `_last_run_status` from GCS storage once on startup."""
        with self._state_lock:
            if self._history_hydrated:
                return
            self._history_hydrated = True

        try:
            runs = storage_service.list_runs(order="asc", limit=200)
            if runs:
                latest = runs[-1]
                with self._state_lock:
                    if self._last_run_at is None:
                        self._last_run_at = (
                            latest.get("completed_at")
                            or latest.get("timestamp")
                            or latest.get("started_at")
                        )
                    if self._last_run_status is None:
                        self._last_run_status = latest.get("status")
        except Exception as exc:
            logger.warning("Could not hydrate initial run status from storage: %s", exc)

    async def _run_adk_synthesizer_async(self, prompt_text: str) -> str:
        """Execute `build_digest_synthesizer_agent()` via Google ADK Runner & InMemorySessionService."""
        agent = build_digest_synthesizer_agent()
        session_service = InMemorySessionService()
        user_id = "haren_scheduler"
        session_id = f"digest_{uuid.uuid4().hex}"

        await session_service.create_session(
            app_name=Config.ADK_APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )

        runner = Runner(
            app_name=Config.ADK_APP_NAME,
            agent=agent,
            session_service=session_service,
        )

        user_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt_text)],
        )

        collected_chunks: list[str] = []
        final_text: str = ""

        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                if event.content and event.content.parts:
                    part_texts = [
                        part.text
                        for part in event.content.parts
                        if getattr(part, "text", None) and not getattr(part, "thought", False)
                    ]
                    if part_texts:
                        chunk = "".join(part_texts).strip()
                        if chunk:
                            collected_chunks.append(chunk)
                            if event.is_final_response():
                                final_text = chunk
        finally:
            close_fn = getattr(runner, "close", None)
            if callable(close_fn):
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro

        if final_text:
            return final_text
        return "\n\n".join(collected_chunks).strip()

    def _run_adk_synthesizer(self, prompt_text: str) -> str:
        """Run the async ADK Runner safely from any synchronous/background thread."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if not in_running_loop:
            return asyncio.run(self._run_adk_synthesizer_async(prompt_text))

        result_holder: dict[str, Any] = {}

        def _worker() -> None:
            try:
                result_holder["output"] = asyncio.run(
                    self._run_adk_synthesizer_async(prompt_text)
                )
            except BaseException as exc:  # noqa: BLE001
                result_holder["error"] = exc

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        worker_thread.join()

        if "error" in result_holder:
            raise result_holder["error"]
        return str(result_holder.get("output") or "")

    def _scheduled_job(self) -> None:
        """Callback invoked by APScheduler on each scheduled interval."""
        if self._run_lock.locked():
            logger.info("Skipping scheduled run because an agent run is already in progress.")
            return
        try:
            self.execute_run(trigger="scheduled", ignore_seen=False)
        except Exception as exc:
            logger.exception("Unhandled error during scheduled Reddit monitor run: %s", exc)

    def init_scheduler(self) -> dict[str, Any]:
        """Initialize the APScheduler BackgroundScheduler idempotently from stored settings."""
        self._hydrate_last_run_from_storage()

        with self._state_lock:
            if self._scheduler_initialized and self._scheduler is not None:
                return self.get_status()

            try:
                settings = storage_service.get_settings()
            except Exception as exc:
                logger.warning("Failed reading settings during init_scheduler, using defaults: %s", exc)
                settings = {}

            interval = max(
                1,
                int(settings.get("schedule_interval_minutes") or Config.SCHEDULE_INTERVAL_MINUTES),
            )
            enabled = bool(
                settings.get("scheduler_enabled", Config.SCHEDULER_AUTO_START)
            )
            self._interval_minutes = interval

            scheduler = BackgroundScheduler(daemon=True, timezone=timezone.utc)
            scheduler.add_job(
                self._scheduled_job,
                trigger="interval",
                minutes=interval,
                id=self.JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            if not enabled:
                scheduler.pause()

            self._scheduler = scheduler
            self._scheduler_initialized = True
            logger.info(
                "Initialized HarenSmartAgent scheduler (enabled=%s, interval=%d min).",
                enabled,
                interval,
            )
            return self.get_status()

    def pause_scheduler(self) -> dict[str, Any]:
        """Pause automatic scheduled runs and persist `scheduler_enabled=False`."""
        with self._state_lock:
            if not self._scheduler_initialized or self._scheduler is None:
                self.init_scheduler()
            if self._scheduler is not None and self._scheduler.state == STATE_RUNNING:
                self._scheduler.pause()
        try:
            storage_service.save_settings({"scheduler_enabled": False})
        except Exception as exc:
            logger.warning("Could not persist scheduler pause setting: %s", exc)
        return self.get_status()

    def resume_scheduler(self) -> dict[str, Any]:
        """Resume automatic scheduled runs and persist `scheduler_enabled=True`."""
        with self._state_lock:
            if not self._scheduler_initialized or self._scheduler is None:
                self.init_scheduler()
            if self._scheduler is not None and self._scheduler.state != STATE_RUNNING:
                self._scheduler.resume()
        try:
            storage_service.save_settings({"scheduler_enabled": True})
        except Exception as exc:
            logger.warning("Could not persist scheduler resume setting: %s", exc)
        return self.get_status()

    def update_interval(self, minutes: int) -> dict[str, Any]:
        """Update the scheduler interval in minutes and persist to settings."""
        clean_minutes = max(1, min(int(minutes), 1440))
        with self._state_lock:
            self._interval_minutes = clean_minutes
            if not self._scheduler_initialized or self._scheduler is None:
                self.init_scheduler()
            if self._scheduler is not None:
                job = self._scheduler.get_job(self.JOB_ID)
                if job is not None:
                    self._scheduler.reschedule_job(
                        self.JOB_ID,
                        trigger="interval",
                        minutes=clean_minutes,
                    )
                else:
                    self._scheduler.add_job(
                        self._scheduled_job,
                        trigger="interval",
                        minutes=clean_minutes,
                        id=self.JOB_ID,
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                    )
        try:
            storage_service.save_settings({"schedule_interval_minutes": clean_minutes})
        except Exception as exc:
            logger.warning("Could not persist updated schedule_interval_minutes: %s", exc)
        return self.get_status()

    def trigger_run_async(
        self,
        trigger: str = "manual",
        ignore_seen: bool = False,
        force_notify: bool = False,
    ) -> tuple[bool, str]:
        """Start an agent run in a background daemon thread if one is not already active."""
        with self._state_lock:
            if self._is_running or self._run_lock.locked():
                return (
                    False,
                    f"An agent run is already in progress (started at {self._current_run_started_at}).",
                )

        def _bg_target() -> None:
            try:
                self.execute_run(
                    trigger=trigger,
                    ignore_seen=ignore_seen,
                    force_notify=force_notify,
                )
            except Exception as exc:
                logger.exception("Background agent run failed: %s", exc)

        thread = threading.Thread(
            target=_bg_target,
            name=f"haren-agent-run-{trigger}",
            daemon=True,
        )
        thread.start()
        return True, f"Started {trigger} Reddit intelligence run in background."

    def execute_run(
        self,
        trigger: str = "manual",
        ignore_seen: bool = False,
        force_notify: bool = False,
    ) -> dict[str, Any]:
        """Execute a complete Reddit fetch, ADK synthesis, Discord notification, and GCS save cycle."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Another Reddit monitor run is currently executing.")

        started_perf = time.monotonic()
        started_dt = datetime.now(timezone.utc)
        started_iso = started_dt.isoformat()
        run_id = f"{started_dt.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

        with self._state_lock:
            self._is_running = True
            self._current_run_started_at = started_iso

        try:
            settings = storage_service.get_settings()
            subreddits: list[str] = list(settings.get("subreddits") or DEFAULT_SUBREDDITS)
            limit_per_sub = max(
                1,
                min(int(settings.get("posts_per_subreddit") or Config.POSTS_PER_SUBREDDIT), 25),
            )
            existing_seen_ids = storage_service.get_seen_post_ids()
            effective_seen_ids: set[str] = set() if ignore_seen else set(existing_seen_ids)

            fetch_result = reddit_fetcher.fetch_multiple_subreddits(
                subreddits=subreddits,
                limit_per_sub=limit_per_sub,
                seen_ids=effective_seen_ids,
                settings=settings,
            )

            all_posts: list[dict[str, Any]] = list(fetch_result.get("all_posts") or [])
            new_posts: list[dict[str, Any]] = list(fetch_result.get("new_posts") or [])
            source_breakdown: dict[str, Any] = dict(fetch_result.get("source_breakdown") or {})
            fetch_errors: list[Any] = list(fetch_result.get("errors") or [])

            candidate_posts = all_posts if ignore_seen else new_posts
            synthesis_error: str | None = None
            raw_agent_response = ""

            if candidate_posts:
                prompt_text = _build_synthesis_prompt(
                    posts=candidate_posts,
                    subreddits=subreddits,
                    total_fetched=len(all_posts),
                    ignore_seen=ignore_seen,
                )
                try:
                    raw_agent_response = self._run_adk_synthesizer(prompt_text)
                    short_summary, full_response = _parse_synthesizer_output(
                        raw_agent_response,
                        fallback_count=len(candidate_posts),
                        subreddits_count=len(subreddits),
                    )
                except Exception as synth_exc:
                    logger.exception("ADK digest synthesizer encountered an error: %s", synth_exc)
                    synthesis_error = str(synth_exc)
                    short_summary, full_response = _build_fallback_digest(
                        posts=candidate_posts,
                        subreddits=subreddits,
                    )
                    full_response += (
                        f"\n\n> _Note: Used structured fallback formatter due to model error: {synthesis_error}_"
                    )

                # Mark all fetched post IDs as seen so future hourly runs only surface fresh posts
                newly_observed_ids = {
                    pid
                    for post in (all_posts + candidate_posts)
                    if (pid := _extract_post_id(post))
                }
                if newly_observed_ids and synthesis_error is None:
                    merged_seen = set(existing_seen_ids) | newly_observed_ids
                    storage_service.save_seen_post_ids(merged_seen)

                should_notify_discord = True
            else:
                if all_posts:
                    short_summary = (
                        f"Checked {len(subreddits)} subreddits ({len(all_posts)} recent posts scanned); "
                        "no brand-new unseen articles appeared since the last check."
                    )
                    full_response = (
                        "### ✅ No Brand-New Unseen Posts Since Last Check\n\n"
                        f"- **Subreddits Checked ({len(subreddits)}):** {', '.join('r/' + s for s in subreddits)}\n"
                        f"- **Recent Posts Scanned:** {len(all_posts)}\n"
                        f"- **Source Breakdown:** `{source_breakdown}`\n\n"
                        "All recent posts across your monitored communities were already analyzed in earlier runs."
                    )
                else:
                    error_suffix = f" ({len(fetch_errors)} fetch errors recorded)" if fetch_errors else ""
                    short_summary = (
                        f"Checked {len(subreddits)} subreddits; 0 posts returned{error_suffix}."
                    )
                    full_response = (
                        "### ⚠️ No Posts Returned From Subreddit Fetch\n\n"
                        f"- **Subreddits Checked ({len(subreddits)}):** {', '.join('r/' + s for s in subreddits)}\n"
                        f"- **Errors:** `{fetch_errors}`"
                    )

                # Skip noisy Discord spam on empty scheduled checks unless forced or manually triggered
                should_notify_discord = bool(force_notify or trigger == "manual")

            completed_iso = _utc_now_iso()
            elapsed_seconds = round(time.monotonic() - started_perf, 2)

            discord_notified = False
            discord_configured, discord_cfg_reason = discord_notifier.is_configured(settings=settings)
            if not should_notify_discord:
                discord_status = "Skipped (no new unseen articles on scheduled check)"
            elif not discord_configured:
                discord_status = f"Not configured ({discord_cfg_reason})"
            else:
                digest_title = (
                    f"🧠 HarenSmartAgent Reddit Digest — {len(candidate_posts)} New Posts"
                    if candidate_posts
                    else "🧠 HarenSmartAgent Manual Check — No New Unseen Posts"
                )
                stats_payload = {
                    "run_id": run_id,
                    "trigger": trigger,
                    "subreddits_count": len(subreddits),
                    "total_fetched": len(all_posts),
                    "new_posts": len(candidate_posts),
                    "duration_seconds": elapsed_seconds,
                    "timestamp": completed_iso,
                }
                try:
                    discord_notified, discord_status = discord_notifier.send_digest(
                        title=digest_title,
                        summary_markdown=full_response,
                        stats=stats_payload,
                        settings=settings,
                    )
                except Exception as disc_exc:
                    logger.exception("Discord notification failed: %s", disc_exc)
                    discord_notified = False
                    discord_status = f"Discord delivery error: {disc_exc}"

            run_status = "success"
            if not all_posts and fetch_errors:
                run_status = "error"
            elif synthesis_error is not None:
                run_status = "partial_error"

            run_record: dict[str, Any] = {
                "run_id": run_id,
                "timestamp": started_iso,
                "started_at": started_iso,
                "completed_at": completed_iso,
                "duration_seconds": elapsed_seconds,
                "trigger": trigger,
                "ignore_seen": ignore_seen,
                "status": run_status,
                "has_new_posts": bool(candidate_posts),
                "model": Config.GEMINI_MODEL,
                "subreddits_checked": subreddits,
                "subreddits_count": len(subreddits),
                "total_posts_fetched": len(all_posts),
                "new_posts_count": len(candidate_posts),
                "short_summary": short_summary,
                "full_response": full_response,
                "raw_agent_response": raw_agent_response,
                "source_breakdown": source_breakdown,
                "fetch_errors": fetch_errors,
                "synthesis_error": synthesis_error,
                "discord_sent": discord_notified,
                "discord_notified": discord_notified,
                "discord_status": discord_status,
                "articles": [_compact_article_record(p) for p in candidate_posts[:MAX_POSTS_IN_PROMPT]],
            }

            saved_record = storage_service.save_run(run_record)
            with self._state_lock:
                self._last_run_at = completed_iso
                self._last_run_status = run_status

            return saved_record or run_record

        except Exception as exc:
            logger.exception("Fatal error during execute_run (%s): %s", run_id, exc)
            completed_iso = _utc_now_iso()
            elapsed_seconds = round(time.monotonic() - started_perf, 2)
            error_record: dict[str, Any] = {
                "run_id": run_id,
                "timestamp": started_iso,
                "started_at": started_iso,
                "completed_at": completed_iso,
                "duration_seconds": elapsed_seconds,
                "trigger": trigger,
                "ignore_seen": ignore_seen,
                "status": "error",
                "has_new_posts": False,
                "model": Config.GEMINI_MODEL,
                "subreddits_checked": list(DEFAULT_SUBREDDITS),
                "subreddits_count": len(DEFAULT_SUBREDDITS),
                "total_posts_fetched": 0,
                "new_posts_count": 0,
                "short_summary": f"Run failed with error: {exc}",
                "full_response": f"### ❌ Agent Run Error\n\n`{exc}`",
                "source_breakdown": {},
                "fetch_errors": [str(exc)],
                "discord_sent": False,
                "discord_notified": False,
                "discord_status": "Skipped due to run error",
                "articles": [],
            }
            try:
                storage_service.save_run(error_record)
            except Exception as save_exc:
                logger.error("Failed saving error run record to storage: %s", save_exc)

            with self._state_lock:
                self._last_run_at = completed_iso
                self._last_run_status = "error"
            return error_record

        finally:
            with self._state_lock:
                self._is_running = False
                self._current_run_started_at = None
            self._run_lock.release()

    def get_status(self) -> dict[str, Any]:
        """Return live runner and APScheduler / Cloud Scheduler status dictionary."""
        import os as _os
        from datetime import timedelta as _timedelta

        self._hydrate_last_run_from_storage()
        cloud_managed = _os.environ.get("CLOUD_SCHEDULER_MANAGED", "").lower() in (
            "1",
            "true",
            "yes",
        )

        if cloud_managed:
            try:
                settings = storage_service.get_settings()
            except Exception:
                settings = {}
            enabled = bool(settings.get("scheduler_enabled", True))
            interval = int(
                settings.get("schedule_interval_minutes") or self._interval_minutes
            )
            next_run_at: str | None = None
            if enabled:
                now_utc = datetime.now(timezone.utc)
                next_hour = now_utc.replace(minute=0, second=0, microsecond=0) + _timedelta(hours=1)
                next_run_at = next_hour.isoformat()

            with self._state_lock:
                return {
                    "is_running": self._is_running,
                    "current_run_started_at": self._current_run_started_at,
                    "scheduler_active": enabled,
                    "interval_minutes": interval,
                    "next_run_at": next_run_at,
                    "last_run_at": self._last_run_at,
                    "last_run_status": self._last_run_status,
                }

        with self._state_lock:
            scheduler_active = bool(
                self._scheduler is not None and self._scheduler.state == STATE_RUNNING
            )
            next_run_at = None
            if scheduler_active and self._scheduler is not None:
                job = self._scheduler.get_job(self.JOB_ID)
                if job is not None and job.next_run_time is not None:
                    next_run_at = job.next_run_time.astimezone(timezone.utc).isoformat()

            return {
                "is_running": self._is_running,
                "current_run_started_at": self._current_run_started_at,
                "scheduler_active": scheduler_active,
                "interval_minutes": self._interval_minutes,
                "next_run_at": next_run_at,
                "last_run_at": self._last_run_at,
                "last_run_status": self._last_run_status,
            }


runner_service = AgentRunnerService()
