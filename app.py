"""Flask Gray-White Private Admin Web Application for HarenSmartAgent."""

from __future__ import annotations

import hmac
import html
import os
import re
import secrets
from datetime import datetime
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config, DEFAULT_SUBREDDITS
from services.discord_notifier import discord_notifier
from services.runner_service import runner_service
from services.storage_service import storage_service

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[assignment]
app.secret_key = Config.FLASK_SECRET_KEY
app.debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE",
        "true" if os.environ.get("K_SERVICE") else "false",
    ).lower()
    in ("1", "true", "yes"),
    SESSION_COOKIE_NAME="haren_smartagent_admin",
)

_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


# ---------------------------------------------------------------------------
# Helpers: Auth, CSRF, Secret Masking, Markdown Formatting & Run Enrichment
# ---------------------------------------------------------------------------


def _is_authenticated() -> bool:
    """Verify active session belongs to the configured Haren admin user."""
    if session.get("authenticated") is not True:
        return False
    sess_user = str(session.get("username") or "")
    return hmac.compare_digest(
        sess_user.encode("utf-8"),
        Config.ADMIN_USERNAME.encode("utf-8"),
    )


def _get_or_create_csrf_token() -> str:
    """Return per-session CSRF token, generating one if absent."""
    token = session.get("csrf_token")
    if not token or not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _mask_secret(value: str | None, keep_tail: int = 4) -> str:
    """Return a masked representation of a secret without exposing full text."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep_tail + 2:
        return "••••••••"
    if raw.startswith("https://discord.com/api/webhooks/") or raw.startswith(
        "https://discordapp.com/api/webhooks/"
    ):
        return f"https://discord.com/api/webhooks/••••••••{raw[-keep_tail:]}"
    return f"••••••••{raw[-keep_tail:]}"


def _resolve_secret_input(
    submitted: str | None,
    existing: str | None,
    clear_requested: bool = False,
) -> str:
    """Keep existing secret if left blank or left as masked placeholder."""
    if clear_requested:
        return ""
    candidate = (submitted or "").strip()
    current = (existing or "").strip()
    if not candidate:
        return current
    if "••••" in candidate or candidate == _mask_secret(current):
        return current
    return candidate


def _format_timestamp(ts: Any) -> str:
    """Format an ISO-8601 timestamp into a crisp human-readable string."""
    if not ts:
        return "—"
    raw = str(ts).strip()
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            offset = dt.strftime("%Z") or "UTC"
            return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {offset}"
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def _format_inline_markdown(escaped_line: str) -> str:
    """Apply safe inline Markdown formatting to an already HTML-escaped line."""
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", escaped_line)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _INLINE_LINK_RE.sub(
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    return text


def render_markdown_safe(markdown_text: str | None) -> str:
    """Convert agent Markdown response into safe, sanitized HTML."""
    raw = (markdown_text or "").strip()
    if not raw:
        return "<p class=\"muted-text\">No detailed Markdown response recorded for this run.</p>"

    lines = raw.splitlines()
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            close_lists()
            if in_code:
                out.append("<pre><code>" + "\n".join(code_buf) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_buf.append(html.escape(line))
            continue

        if not stripped:
            close_lists()
            continue

        escaped = html.escape(stripped)

        if stripped in ("---", "***", "___"):
            close_lists()
            out.append("<hr>")
        elif stripped.startswith("### "):
            close_lists()
            out.append(f"<h3>{_format_inline_markdown(html.escape(stripped[4:]))}</h3>")
        elif stripped.startswith("## "):
            close_lists()
            out.append(f"<h2>{_format_inline_markdown(html.escape(stripped[3:]))}</h2>")
        elif stripped.startswith("# "):
            close_lists()
            out.append(f"<h2>{_format_inline_markdown(html.escape(stripped[2:]))}</h2>")
        elif stripped.startswith(("- ", "* ", "• ")):
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            item_text = stripped[2:].strip()
            out.append(f"<li>{_format_inline_markdown(html.escape(item_text))}</li>")
        elif re.match(r"^\d+\.\s+", stripped):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            item_text = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f"<li>{_format_inline_markdown(html.escape(item_text))}</li>")
        elif stripped.startswith(">"):
            close_lists()
            quote_text = stripped.lstrip(">").strip()
            out.append(
                f"<blockquote>{_format_inline_markdown(html.escape(quote_text))}</blockquote>"
            )
        else:
            close_lists()
            out.append(f"<p>{_format_inline_markdown(escaped)}</p>")

    close_lists()
    if in_code and code_buf:
        out.append("<pre><code>" + "\n".join(code_buf) + "</code></pre>")

    return "\n".join(out)


def _derive_source_badge(run: dict[str, Any]) -> str:
    """Summarize MCP vs backup fetch source breakdown cleanly."""
    breakdown = run.get("source_breakdown")
    if isinstance(breakdown, dict) and breakdown:
        mcp_count = 0
        backup_count = 0
        for key, val in breakdown.items():
            try:
                count = int(val)
            except (TypeError, ValueError):
                continue
            lowered = str(key).lower()
            if "mcp" in lowered:
                mcp_count += count
            elif any(
                tok in lowered
                for tok in ("backup", "fallback", "http", "json", "rss", "direct")
            ):
                backup_count += count
        if mcp_count > 0 and backup_count == 0:
            return f"MCP ({mcp_count})"
        if mcp_count > 0 and backup_count > 0:
            return f"MCP ({mcp_count}) · Backup ({backup_count})"
        if backup_count > 0:
            return f"Backup ({backup_count})"
    if isinstance(breakdown, str) and breakdown.strip():
        return breakdown.strip()
    return str(run.get("connector_source") or "MCP")


def _enrich_run(run: dict[str, Any], bucket_name: str) -> dict[str, Any]:
    """Normalize a run record for consistent table and modal presentation."""
    enriched = dict(run)
    run_id = str(enriched.get("run_id") or enriched.get("id") or "unknown")
    enriched["run_id"] = run_id

    started_raw = (
        enriched.get("started_at")
        or enriched.get("timestamp")
        or enriched.get("created_at")
        or ""
    )
    enriched["display_started_at"] = _format_timestamp(started_raw)
    enriched["display_completed_at"] = _format_timestamp(
        enriched.get("completed_at") or enriched.get("finished_at")
    )

    raw_gcs = str(enriched.get("gcs_uri") or enriched.get("gcs_blob_path") or "").strip()
    if raw_gcs.startswith("gs://"):
        gcs_uri = raw_gcs
    elif raw_gcs:
        gcs_uri = f"gs://{bucket_name}/{raw_gcs.lstrip('/')}"
    else:
        prefix = Config.GCS_PREFIX.strip("/")
        gcs_uri = f"gs://{bucket_name}/{prefix}/runs/{run_id}.json"
    enriched["gcs_uri"] = gcs_uri

    enriched["trigger"] = str(enriched.get("trigger") or "scheduled")
    enriched["status"] = str(enriched.get("status") or "unknown")
    enriched["new_posts_count"] = int(enriched.get("new_posts_count") or 0)
    enriched["total_posts_fetched"] = int(enriched.get("total_posts_fetched") or 0)
    enriched["source_badge"] = _derive_source_badge(enriched)

    discord_sent = bool(enriched.get("discord_sent") or enriched.get("discord_notified"))
    discord_status = str(enriched.get("discord_status") or "").strip()
    if not discord_status:
        discord_status = "Sent" if discord_sent else "Not sent"
    enriched["discord_sent"] = discord_sent
    enriched["discord_status"] = discord_status

    full_response = str(
        enriched.get("full_response")
        or enriched.get("markdown_summary")
        or enriched.get("summary")
        or ""
    ).strip()
    short_summary = str(enriched.get("short_summary") or "").strip()
    if not short_summary and full_response:
        plain = re.sub(r"[#*`>_]+", "", full_response)
        plain = " ".join(plain.split())
        short_summary = plain[:220] + ("…" if len(plain) > 220 else "")
    elif not short_summary:
        short_summary = "No new AI/ML posts required summarization in this window."

    enriched["short_summary"] = short_summary
    enriched["full_response"] = full_response or short_summary
    return enriched


def _current_order() -> str:
    """Resolve active sort order ('asc' chronological default, or 'desc')."""
    candidate = (
        request.args.get("order") or request.form.get("order") or "asc"
    ).strip().lower()
    return "desc" if candidate == "desc" else "asc"


# ---------------------------------------------------------------------------
# Request Hooks & Template Context
# ---------------------------------------------------------------------------


@app.before_request
def enforce_auth_and_csrf() -> Any:
    """Require Haren authentication on all routes (except /login, /healthz, /api/cron/hourly & /static/*) and validate CSRF."""
    endpoint = request.endpoint or ""
    if endpoint in ("static", "healthz", "cron_hourly"):
        return None

    if not _is_authenticated():
        if endpoint != "login":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required."}), 401
            return redirect(url_for("login"))

    if request.method == "POST":
        submitted_token = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        expected_token = session.get("csrf_token") or ""
        if (
            not expected_token
            or not submitted_token
            or not hmac.compare_digest(str(submitted_token), str(expected_token))
        ):
            abort(400, description="Invalid or missing CSRF token.")
    return None


@app.after_request
def apply_security_headers(response: Any) -> Any:
    """Attach strict private-admin security headers."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint != "static":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.context_processor
def inject_template_globals() -> dict[str, Any]:
    """Provide CSRF token helper and authenticated admin identity to templates."""
    return {
        "csrf_token": _get_or_create_csrf_token,
        "admin_username": session.get("username") or Config.ADMIN_USERNAME,
        "is_authenticated": _is_authenticated(),
    }


# ---------------------------------------------------------------------------
# Authentication Routes
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Render and process Haren-only administrator sign-in."""
    if _is_authenticated():
        return redirect(url_for("dashboard"))

    # Ensure a CSRF token is stored in the session prior to rendering the form
    _get_or_create_csrf_token()

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user_ok = hmac.compare_digest(
            username.encode("utf-8"),
            Config.ADMIN_USERNAME.encode("utf-8"),
        )
        pass_ok = hmac.compare_digest(
            password.encode("utf-8"),
            Config.ADMIN_PASSWORD.encode("utf-8"),
        )

        if user_ok and pass_ok:
            session.clear()
            session["authenticated"] = True
            session["username"] = Config.ADMIN_USERNAME
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("dashboard"))

        flash("Invalid administrator credentials. Access is restricted to Haren.", "error")
        return render_template("login.html"), 401

    return render_template("login.html")


@app.post("/logout")
def logout() -> Any:
    """Terminate Haren admin session."""
    session.clear()
    flash("Signed out of HarenSmartAgent.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Primary Dashboard Route
# ---------------------------------------------------------------------------


@app.get("/")
def dashboard() -> Any:
    """Render private Haren admin dashboard with chronological agent run summaries."""
    order = _current_order()
    storage_health = storage_service.storage_health()
    bucket_name = str(storage_health.get("bucket") or Config.GCS_BUCKET_NAME)

    settings = storage_service.get_settings()
    runner_status = runner_service.get_status()
    discord_ready, discord_summary = discord_notifier.is_configured(settings)

    raw_runs = storage_service.list_runs(order=order, limit=200)
    runs = [_enrich_run(r, bucket_name) for r in raw_runs]

    masked_secrets = {
        "discord_webhook_url": _mask_secret(settings.get("discord_webhook_url")),
        "discord_bot_token": _mask_secret(settings.get("discord_bot_token")),
        "reddit_client_id": _mask_secret(settings.get("reddit_client_id")),
        "reddit_client_secret": _mask_secret(settings.get("reddit_client_secret")),
    }

    formatted_runner = dict(runner_status)
    formatted_runner["display_next_run_at"] = _format_timestamp(
        runner_status.get("next_run_at")
    )
    formatted_runner["display_last_run_at"] = _format_timestamp(
        runner_status.get("last_run_at")
    )
    formatted_runner["display_current_started_at"] = _format_timestamp(
        runner_status.get("current_run_started_at")
    )

    return render_template(
        "dashboard.html",
        order=order,
        runs=runs,
        storage_health=storage_health,
        settings=settings,
        masked_secrets=masked_secrets,
        runner_status=formatted_runner,
        discord_ready=discord_ready,
        discord_summary=discord_summary,
        default_subreddits=DEFAULT_SUBREDDITS,
        gemini_model=Config.GEMINI_MODEL,
    )


# ---------------------------------------------------------------------------
# Admin Operations POST Routes
# ---------------------------------------------------------------------------


@app.post("/actions/run-now")
def run_now() -> Any:
    """Trigger an asynchronous background agent run immediately."""
    order = _current_order()
    ignore_seen = (request.form.get("ignore_seen") or "").lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    started, message = runner_service.trigger_run_async(
        trigger="manual",
        ignore_seen=ignore_seen,
    )
    flash(message, "success" if started else "warning")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/scheduler")
def update_scheduler() -> Any:
    """Pause, resume, or update the interval of the background scheduler."""
    order = _current_order()
    action = (request.form.get("action") or "").strip().lower()

    if action == "pause":
        runner_service.pause_scheduler()
        storage_service.save_settings({"scheduler_enabled": False})
        flash("Background scheduler paused.", "info")
    elif action == "resume":
        runner_service.resume_scheduler()
        storage_service.save_settings({"scheduler_enabled": True})
        flash("Background scheduler resumed.", "success")
    elif action == "interval":
        raw_minutes = (request.form.get("schedule_interval_minutes") or "60").strip()
        try:
            minutes = int(raw_minutes)
            if minutes < 5 or minutes > 1440:
                raise ValueError("Interval out of range")
        except ValueError:
            flash("Schedule interval must be a whole number between 5 and 1440 minutes.", "error")
            return redirect(url_for("dashboard", order=order))

        runner_service.update_interval(minutes)
        storage_service.save_settings({"schedule_interval_minutes": minutes})
        flash(f"Scheduler interval updated to every {minutes} minutes.", "success")
    else:
        flash("Unrecognized scheduler command.", "error")

    return redirect(url_for("dashboard", order=order))


def _collect_discord_updates(existing: dict[str, Any]) -> dict[str, Any]:
    """Build safe Discord configuration updates preserving masked/blank secrets."""
    clear_secrets = (request.form.get("clear_discord_secrets") or "").lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    webhook_url = _resolve_secret_input(
        request.form.get("discord_webhook_url"),
        existing.get("discord_webhook_url"),
        clear_requested=clear_secrets,
    )
    bot_token = _resolve_secret_input(
        request.form.get("discord_bot_token"),
        existing.get("discord_bot_token"),
        clear_requested=clear_secrets,
    )
    channel_id = (request.form.get("discord_channel_id") or "").strip()
    user_id = (request.form.get("discord_user_id") or "").strip()

    if clear_secrets:
        channel_id = ""
        user_id = ""

    return {
        "discord_webhook_url": webhook_url,
        "discord_bot_token": bot_token,
        "discord_channel_id": channel_id,
        "discord_user_id": user_id,
    }


@app.post("/actions/discord")
def save_discord() -> Any:
    """Persist Discord notification settings into GCS-backed configuration."""
    order = _current_order()
    existing = storage_service.get_settings()
    updates = _collect_discord_updates(existing)
    saved = storage_service.save_settings(updates)
    ok, detail = discord_notifier.is_configured(saved)
    if ok:
        flash(f"Discord configuration saved ({detail}).", "success")
    else:
        flash("Discord settings updated (notifications currently disabled until credentials are provided).", "info")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/discord/test")
def test_discord() -> Any:
    """Save any newly entered Discord credentials and send a live test notification."""
    order = _current_order()
    existing = storage_service.get_settings()
    updates = _collect_discord_updates(existing)
    saved = storage_service.save_settings(updates)
    ok, message = discord_notifier.send_test_message(saved)
    flash(message, "success" if ok else "error")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/subreddits/add")
def add_subreddits() -> Any:
    """Add one or more subreddits to the monitored list."""
    order = _current_order()
    raw_input = (request.form.get("new_subreddits") or "").strip()
    if not raw_input:
        flash("Enter at least one subreddit name to add.", "warning")
        return redirect(url_for("dashboard", order=order))

    settings = storage_service.get_settings()
    current: list[str] = list(settings.get("subreddits") or DEFAULT_SUBREDDITS)
    existing_lower = {s.lower() for s in current}

    tokens = [t.strip() for t in re.split(r"[,\s]+", raw_input) if t.strip()]
    added: list[str] = []
    invalid: list[str] = []

    for token in tokens:
        cleaned = re.sub(r"^/?r/", "", token, flags=re.IGNORECASE).strip("/")
        if not _SUBREDDIT_RE.match(cleaned):
            invalid.append(token)
            continue
        if cleaned.lower() not in existing_lower:
            current.append(cleaned)
            existing_lower.add(cleaned.lower())
            added.append(cleaned)

    if added:
        storage_service.save_settings({"subreddits": current})
        joined = ", ".join(f"r/{s}" for s in added)
        flash(f"Added {joined} to monitored subreddits.", "success")
    elif not invalid:
        flash("Subreddit is already in the monitored list.", "info")

    if invalid:
        flash(
            f"Skipped invalid subreddit name(s): {', '.join(invalid)}.",
            "warning",
        )

    return redirect(url_for("dashboard", order=order))


@app.post("/actions/subreddits/remove")
def remove_subreddit() -> Any:
    """Remove a subreddit from the monitored list."""
    order = _current_order()
    target = (request.form.get("subreddit") or "").strip()
    settings = storage_service.get_settings()
    current: list[str] = list(settings.get("subreddits") or DEFAULT_SUBREDDITS)
    updated = [s for s in current if s.lower() != target.lower()]

    if not updated:
        flash("At least one subreddit must remain monitored.", "warning")
        return redirect(url_for("dashboard", order=order))

    if len(updated) != len(current):
        storage_service.save_settings({"subreddits": updated})
        flash(f"Removed r/{target} from monitored subreddits.", "info")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/subreddits/reset")
def reset_subreddits() -> Any:
    """Restore the default 14 AI/ML subreddits."""
    order = _current_order()
    storage_service.save_settings({"subreddits": list(DEFAULT_SUBREDDITS)})
    flash(f"Restored default {len(DEFAULT_SUBREDDITS)} monitored subreddits.", "success")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/reddit-connector")
def save_reddit_connector() -> Any:
    """Save posts-per-subreddit and optional Reddit API credentials for MCP."""
    order = _current_order()
    existing = storage_service.get_settings()

    raw_limit = (request.form.get("posts_per_subreddit") or "8").strip()
    try:
        posts_per_sub = int(raw_limit)
        if posts_per_sub < 1 or posts_per_sub > 25:
            raise ValueError("Out of range")
    except ValueError:
        flash("Posts per subreddit must be between 1 and 25.", "error")
        return redirect(url_for("dashboard", order=order))

    clear_creds = (request.form.get("clear_reddit_secrets") or "").lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    client_id = _resolve_secret_input(
        request.form.get("reddit_client_id"),
        existing.get("reddit_client_id"),
        clear_requested=clear_creds,
    )
    client_secret = _resolve_secret_input(
        request.form.get("reddit_client_secret"),
        existing.get("reddit_client_secret"),
        clear_requested=clear_creds,
    )

    storage_service.save_settings(
        {
            "posts_per_subreddit": posts_per_sub,
            "reddit_client_id": client_id,
            "reddit_client_secret": client_secret,
        }
    )
    flash("Reddit connector settings saved (strict read-only mode active).", "success")
    return redirect(url_for("dashboard", order=order))


@app.post("/actions/clear-seen")
def clear_seen_posts() -> Any:
    """Clear stored seen-post IDs so future runs re-evaluate recent posts."""
    order = _current_order()
    storage_service.clear_seen_post_ids()
    flash("Cleared seen-posts deduplication cache.", "success")
    return redirect(url_for("dashboard", order=order))


# ---------------------------------------------------------------------------
# Authenticated JSON Endpoints for Live Status Poller & Modal Report Viewer
# ---------------------------------------------------------------------------


@app.get("/api/status")
def api_status() -> Any:
    """Return live runner & scheduler status for dashboard polling."""
    status = dict(runner_service.get_status())
    status["display_next_run_at"] = _format_timestamp(status.get("next_run_at"))
    status["display_last_run_at"] = _format_timestamp(status.get("last_run_at"))
    status["display_current_started_at"] = _format_timestamp(
        status.get("current_run_started_at")
    )
    return jsonify(status)


@app.get("/api/runs/<run_id>")
def api_run_detail(run_id: str) -> Any:
    """Return complete run payload and rendered HTML summary for the modal dialog."""
    run = storage_service.get_run(run_id)
    if not run:
        return jsonify({"error": f"Run '{run_id}' not found."}), 404

    storage_health = storage_service.storage_health()
    bucket_name = str(storage_health.get("bucket") or Config.GCS_BUCKET_NAME)
    enriched = _enrich_run(run, bucket_name)
    enriched["rendered_html"] = render_markdown_safe(enriched.get("full_response"))
    return jsonify(enriched)


@app.get("/healthz")
def healthz() -> Any:
    """Lightweight container readiness probe for Cloud Run."""
    return jsonify({"status": "ok"}), 200


@app.post("/api/cron/hourly")
def cron_hourly() -> Any:
    """Secure Cloud Scheduler webhook to execute hourly monitoring synchronously on Cloud Run."""
    expected_secret = (Config.CRON_SECRET or "").strip()
    provided_secret = (request.headers.get("X-Cron-Secret") or "").strip()
    if not expected_secret or not provided_secret or not hmac.compare_digest(
        provided_secret.encode("utf-8"),
        expected_secret.encode("utf-8"),
    ):
        return jsonify({"error": "Forbidden"}), 403

    settings = storage_service.get_settings()
    if not settings.get("scheduler_enabled", True):
        return jsonify({"status": "skipped", "reason": "scheduler_paused_in_admin_ui"}), 200

    result = runner_service.execute_run(trigger="scheduled", ignore_seen=False)
    return jsonify(
        {
            "run_id": result.get("run_id"),
            "status": result.get("status"),
            "new_posts_count": result.get("new_posts_count"),
            "total_posts_fetched": result.get("total_posts_fetched"),
            "discord_sent": result.get("discord_sent"),
            "gcs_blob_path": result.get("gcs_blob_path"),
        }
    ), 200


# ---------------------------------------------------------------------------
# Scheduler Startup Guard (Avoid Duplicate Schedulers Under Debug Reloader)
# ---------------------------------------------------------------------------

if (
    os.environ.get("SKIP_SCHEDULER_INIT") != "1"
    and os.environ.get("CLOUD_SCHEDULER_MANAGED", "").lower() not in ("1", "true", "yes")
):
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        runner_service.init_scheduler()


if __name__ == "__main__":
    app.run(
        host=Config.FLASK_HOST,
        port=Config.FLASK_PORT,
        debug=app.debug,
    )
