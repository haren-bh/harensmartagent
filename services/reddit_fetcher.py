"""Strictly READ-ONLY Reddit post fetcher using Reddit MCP Server + resilient backup parser.

SAFETY GUARANTEE:
This module is strictly READ-ONLY. It NEVER creates posts, comments, votes, messages,
or modifies any state on Reddit under any circumstances.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from config import Config, resolve_mcp_command
    from services.storage_service import normalize_subreddit_name
except ImportError:  # pragma: no cover - package-relative import fallback
    from ..config import Config, resolve_mcp_command  # type: ignore[no-redef]
    from .storage_service import normalize_subreddit_name  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class McpConnectorError(RuntimeError):
    """Raised when the primary Reddit MCP connector returns an error or HTTP 403 block."""


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def _normalize_permalink(raw_permalink: str, subreddit: str) -> str:
    raw = (raw_permalink or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw:
        return "https://www.reddit.com" + (raw if raw.startswith("/") else f"/{raw}")
    return f"https://www.reddit.com/r/{subreddit}/"


def _extract_post_id(raw_id: str | None, permalink: str, title: str = "") -> str:
    if raw_id:
        cleaned = str(raw_id).strip()
        if cleaned:
            return cleaned if cleaned.startswith("t3_") else f"t3_{cleaned}"
    match = re.search(r"/comments/([a-z0-9]+)/", permalink, flags=re.IGNORECASE)
    if match:
        return f"t3_{match.group(1)}"
    digest = hashlib.sha1(f"{permalink}|{title}".encode("utf-8")).hexdigest()[:10]
    return f"t3_{digest}"


def _format_created_utc(raw_created: Any) -> str:
    if raw_created is None or raw_created == "":
        return ""
    if isinstance(raw_created, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw_created), tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return str(raw_created)
    raw_str = str(raw_created).strip()
    try:
        as_float = float(raw_str)
        return datetime.fromtimestamp(as_float, tz=timezone.utc).isoformat()
    except ValueError:
        return raw_str


def _run_coro_sync(coro: Any, timeout: float = 25.0) -> Any:
    """Execute an async coroutine safely whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False

    if not loop_running:
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: asyncio.run(asyncio.wait_for(coro, timeout=timeout)))
        return future.result(timeout=timeout + 2.0)


class RedditFetcher:
    """Fetches newest subreddit posts via eliasbiondo/reddit-mcp-server with automatic backup."""

    READ_ONLY_MODE: bool = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cffi_session: cffi_requests.Session | None = None
        self._readonly_cookie_attempted: bool = False

    # ------------------------------------------------------------------
    # Primary Connector: eliasbiondo/reddit-mcp-server (MCP stdio)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_mcp_env(settings: dict[str, Any] | None = None) -> dict[str, str]:
        """Build environment for reddit-no-auth-mcp-server.

        IMPORTANT: Pass REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT
        ONLY when BOTH client_id and client_secret are non-empty. Setting only one
        raises a ConfigurationError in reddit-no-auth-mcp-server.
        """
        env = dict(os.environ)
        env.pop("REDDIT_CLIENT_ID", None)
        env.pop("REDDIT_CLIENT_SECRET", None)
        env.pop("REDDIT_USER_AGENT", None)

        cfg = settings or {}
        client_id = str(cfg.get("reddit_client_id") or Config.REDDIT_CLIENT_ID or "").strip()
        client_secret = str(
            cfg.get("reddit_client_secret") or Config.REDDIT_CLIENT_SECRET or ""
        ).strip()

        if client_id and client_secret:
            env["REDDIT_CLIENT_ID"] = client_id
            env["REDDIT_CLIENT_SECRET"] = client_secret
            env["REDDIT_USER_AGENT"] = Config.REDDIT_USER_AGENT

        return env

    @staticmethod
    def _parse_mcp_tool_result(result: Any, subreddit: str, limit: int) -> list[dict[str, Any]]:
        """Validate MCP CallToolResult and parse returned posts into canonical dicts."""
        is_error = bool(getattr(result, "isError", False))

        text_chunks: list[str] = []
        for content_item in getattr(result, "content", None) or []:
            txt = getattr(content_item, "text", None)
            if isinstance(txt, str) and txt:
                text_chunks.append(txt)
        combined_text = "\n".join(text_chunks).strip()

        if (
            is_error
            or "HTTP 403" in combined_text
            or "Reddit refused" in combined_text
            or "HTTP 429" in combined_text
        ):
            raise McpConnectorError(
                combined_text or f"MCP tool returned error for r/{subreddit}"
            )

        structured = getattr(result, "structuredContent", None)
        payload: Any = structured
        if payload is None and combined_text:
            try:
                payload = json.loads(combined_text)
            except json.JSONDecodeError as exc:
                raise McpConnectorError(
                    f"Non-JSON MCP output for r/{subreddit}: {combined_text[:200]}"
                ) from exc

        if isinstance(payload, dict):
            raw_posts = payload.get("posts")
            if raw_posts is None and isinstance(payload.get("result"), dict):
                raw_posts = payload["result"].get("posts")
            if raw_posts is None and isinstance(payload.get("results"), list):
                raw_posts = payload.get("results")
        elif isinstance(payload, list):
            raw_posts = payload
        else:
            raw_posts = None

        if not isinstance(raw_posts, list):
            raise McpConnectorError(f"Unexpected MCP payload structure for r/{subreddit}")

        posts: list[dict[str, Any]] = []
        for item in raw_posts[:limit]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            sub_name = str(item.get("subreddit") or subreddit).strip() or subreddit
            permalink = _normalize_permalink(str(item.get("permalink") or ""), sub_name)
            url = str(item.get("url") or "").strip() or permalink
            post_id = _extract_post_id(
                item.get("id") or item.get("name"), permalink=permalink, title=title
            )
            selftext = str(item.get("selftext") or "").strip()[:400]

            posts.append(
                {
                    "id": post_id,
                    "subreddit": sub_name,
                    "title": title,
                    "permalink": permalink,
                    "url": url,
                    "author": str(item.get("author") or "unknown").strip() or "unknown",
                    "score": _safe_int(item.get("score"), 0),
                    "num_comments": _safe_int(item.get("num_comments"), 0),
                    "created_utc": _format_created_utc(item.get("created_utc")),
                    "selftext": selftext,
                    "source": "mcp",
                }
            )
        return posts

    async def _fetch_via_mcp_async(
        self,
        subreddit: str,
        limit: int = 8,
        settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        command, args = resolve_mcp_command()
        env = self._build_mcp_env(settings)
        server_params = StdioServerParameters(command=command, args=args, env=env)

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "reddit_get_subreddit_posts",
                    {"subreddit": subreddit, "limit": int(limit), "category": "new"},
                )
                return self._parse_mcp_tool_result(result, subreddit=subreddit, limit=limit)

    def _fetch_via_mcp(
        self,
        subreddit: str,
        limit: int = 8,
        settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return _run_coro_sync(
            self._fetch_via_mcp_async(subreddit=subreddit, limit=limit, settings=settings),
            timeout=25.0,
        )

    # ------------------------------------------------------------------
    # Backup Connector: Standard requests (.json) -> curl_cffi + shreddit HTML
    # ------------------------------------------------------------------

    @staticmethod
    def _try_standard_requests_json(subreddit: str, limit: int = 8) -> list[dict[str, Any]] | None:
        """Attempt standard requests GET against /r/{subreddit}/new.json."""
        url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={int(limit)}&raw_json=1"
        with requests.Session() as std_sess:
            std_sess.headers.update(
                {
                    "User-Agent": Config.REDDIT_USER_AGENT,
                    "Accept": "application/json",
                }
            )
            try:
                resp = std_sess.get(url, timeout=8)
                if resp.status_code != 200:
                    return None
                payload = resp.json()
            except (requests.RequestException, ValueError):
                return None

        children = (
            payload.get("data", {}).get("children") if isinstance(payload, dict) else None
        )
        if not isinstance(children, list):
            return None

        posts: list[dict[str, Any]] = []
        for child in children[:limit]:
            if not isinstance(child, dict):
                continue
            data = child.get("data")
            if not isinstance(data, dict):
                continue
            title = str(data.get("title") or "").strip()
            sub_name = str(data.get("subreddit") or subreddit).strip() or subreddit
            permalink = _normalize_permalink(str(data.get("permalink") or ""), sub_name)
            content_url = (
                str(data.get("url_overridden_by_dest") or data.get("url") or "").strip()
                or permalink
            )
            post_id = _extract_post_id(
                data.get("name") or data.get("id"), permalink=permalink, title=title
            )
            selftext = str(data.get("selftext") or "").strip()[:400]

            posts.append(
                {
                    "id": post_id,
                    "subreddit": sub_name,
                    "title": title,
                    "permalink": permalink,
                    "url": content_url,
                    "author": str(data.get("author") or "unknown").strip() or "unknown",
                    "score": _safe_int(data.get("score"), 0),
                    "num_comments": _safe_int(data.get("num_comments"), 0),
                    "created_utc": _format_created_utc(data.get("created_utc")),
                    "selftext": selftext,
                    "source": "requests_backup",
                }
            )
        return posts

    def _get_shared_cffi_session(self) -> cffi_requests.Session:
        with self._lock:
            if self._cffi_session is None:
                self._cffi_session = cffi_requests.Session(impersonate="chrome")
            return self._cffi_session

    @staticmethod
    def _solve_js_challenge_if_present(
        session: cffi_requests.Session, html: str, base_url: str = "https://www.reddit.com"
    ) -> str | None:
        """Detect and solve Reddit's lightweight JS challenge page on a curl_cffi session."""
        m = re.search(r'await\(async\s+\w+=>([^)]+)\)\("([0-9a-f]+)"\)', html)
        if not m:
            return None

        expr, seed = m.group(1).strip(), m.group(2)
        if "+" in expr:
            solution = seed + seed
        elif "reverse" in expr:
            solution = seed[::-1]
        else:
            solution = seed

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form")
        if not form:
            return None

        action = urljoin(base_url, str(form.get("action") or "/"))
        params: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                params[str(name)] = str(inp.get("value") or "")

        params["solution"] = solution
        params.setdefault("js_challenge", "1")
        params.setdefault("jsc_token", "")
        params.setdefault("jsc_orig_r", "")

        resp = session.get(action, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None

    def _attempt_readonly_cookie_login(self, session: cffi_requests.Session) -> bool:
        """Optional strictly READ-ONLY session cookie acquisition if unauthenticated HTML fails."""
        username = (Config.REDDIT_USERNAME or "").strip()
        password = (Config.REDDIT_PASSWORD or "").strip()
        if not username or not password or self._readonly_cookie_attempted:
            return False

        self._readonly_cookie_attempted = True
        login_url = f"https://old.reddit.com/api/login/{username}"
        try:
            resp = session.post(
                login_url,
                data={
                    "op": "login-main",
                    "user": username,
                    "passwd": password,
                    "api_type": "json",
                },
                headers={
                    "Referer": "https://old.reddit.com/",
                    "Origin": "https://old.reddit.com",
                },
                timeout=12,
            )
            return resp.status_code == 200 and "reddit_session" in session.cookies.get_dict()
        except Exception as exc:
            logger.debug("Optional read-only cookie login did not succeed: %s", exc)
            return False

    @staticmethod
    def _parse_shreddit_posts(
        html: str, subreddit: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Parse <shreddit-post> elements from Reddit HTML into canonical post dicts."""
        if "<shreddit-post" not in html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        elements = soup.find_all("shreddit-post")
        posts: list[dict[str, Any]] = []

        for post in elements[:limit]:
            raw_permalink = str(post.get("permalink") or "").strip()
            if raw_permalink.startswith("http://") or raw_permalink.startswith("https://"):
                permalink = raw_permalink
            else:
                permalink = "https://www.reddit.com" + raw_permalink

            title = str(post.get("post-title", "") or "").strip()
            sub_name = str(post.get("subreddit-name") or subreddit).strip() or subreddit
            raw_id = post.get("id")
            post_id = str(raw_id).strip() if raw_id else _extract_post_id(None, permalink, title)
            content_url = str(post.get("content-href") or "").strip() or permalink
            author = str(post.get("author", "unknown") or "unknown").strip() or "unknown"
            score = _safe_int(post.get("score") or 0, 0)
            num_comments = _safe_int(post.get("comment-count") or 0, 0)
            created_utc = str(post.get("created-timestamp", "") or "").strip()

            text_node = post.select_one('[slot="text-body"]')
            selftext = text_node.get_text(" ", strip=True)[:400] if text_node else ""

            posts.append(
                {
                    "id": post_id,
                    "subreddit": sub_name,
                    "title": title,
                    "permalink": permalink,
                    "url": content_url,
                    "author": author,
                    "score": score,
                    "num_comments": num_comments,
                    "created_utc": created_utc,
                    "selftext": selftext,
                    "source": "requests_backup",
                }
            )

        return posts

    def _fetch_via_cffi_shreddit(
        self,
        subreddit: str,
        limit: int = 8,
        session: cffi_requests.Session | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch subreddit new posts via curl_cffi Chrome TLS impersonation + shreddit HTML."""
        sess = session or self._get_shared_cffi_session()
        more_posts_url = (
            f"https://www.reddit.com/svc/shreddit/community-more-posts/new/?name={subreddit}"
        )
        fallback_listing_url = f"https://www.reddit.com/r/{subreddit}/new/"

        def _get_resolved_html(target_url: str) -> tuple[int, str]:
            resp = sess.get(target_url, timeout=15)
            html_text = resp.text or ""
            if "<shreddit-post" not in html_text:
                solved_html = self._solve_js_challenge_if_present(sess, html_text)
                if solved_html is not None:
                    # If challenge redirected to listing page, re-request target_url if needed
                    if "<shreddit-post" in solved_html and target_url == fallback_listing_url:
                        return 200, solved_html
                    resp = sess.get(target_url, timeout=15)
                    html_text = resp.text or ""
            return resp.status_code, html_text

        status_code, html = _get_resolved_html(more_posts_url)
        posts = self._parse_shreddit_posts(html, subreddit=subreddit, limit=limit)
        if posts:
            return posts

        # Fallback to full /r/{subreddit}/new/ page (also triggers challenge form if not yet solved)
        status_code_fb, html_fb = _get_resolved_html(fallback_listing_url)
        posts = self._parse_shreddit_posts(html_fb, subreddit=subreddit, limit=limit)
        if posts:
            return posts

        # Retry community-more-posts once more if the fallback page just solved the JS challenge
        if status_code != 200 and status_code_fb == 200:
            _, html_retry = _get_resolved_html(more_posts_url)
            posts = self._parse_shreddit_posts(html_retry, subreddit=subreddit, limit=limit)
            if posts:
                return posts

        # Optional strictly read-only cookie login fallback if unauthenticated HTML was blocked
        if status_code_fb in (401, 403, 429) or (status_code != 200 and not posts):
            if self._attempt_readonly_cookie_login(sess):
                _, html_auth = _get_resolved_html(more_posts_url)
                posts = self._parse_shreddit_posts(html_auth, subreddit=subreddit, limit=limit)
                if posts:
                    return posts
                _, html_auth_fb = _get_resolved_html(fallback_listing_url)
                posts = self._parse_shreddit_posts(html_auth_fb, subreddit=subreddit, limit=limit)
                if posts:
                    return posts

        if status_code not in (200,) and status_code_fb not in (200,):
            raise RuntimeError(
                f"Reddit returned HTTP {status_code_fb} for r/{subreddit} in backup fetcher."
            )

        return posts

    def fetch_via_requests_backup(
        self,
        subreddit: str,
        limit: int = 8,
        *,
        skip_std_requests: bool = False,
        cffi_session: cffi_requests.Session | None = None,
    ) -> list[dict[str, Any]]:
        """Backup connector: tries standard requests .json first, then curl_cffi + shreddit HTML."""
        clean_sub = normalize_subreddit_name(subreddit) or subreddit.strip()
        if not clean_sub:
            raise ValueError("Subreddit name cannot be empty.")

        if not skip_std_requests:
            std_posts = self._try_standard_requests_json(clean_sub, limit=limit)
            if std_posts is not None:
                return std_posts

        return self._fetch_via_cffi_shreddit(
            clean_sub,
            limit=limit,
            session=cffi_session,
        )

    # ------------------------------------------------------------------
    # Unified Single & Multi-Subreddit Fetch Methods
    # ------------------------------------------------------------------

    def fetch_subreddit_new(
        self,
        subreddit: str,
        limit: int = 8,
        settings: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Fetch newest posts for a single subreddit via MCP primary or requests backup."""
        clean_sub = normalize_subreddit_name(subreddit) or subreddit.strip()
        if not clean_sub:
            raise ValueError("Subreddit name cannot be empty.")

        try:
            mcp_posts = self._fetch_via_mcp(clean_sub, limit=limit, settings=settings)
            return mcp_posts, "mcp"
        except Exception as mcp_exc:
            logger.info(
                "MCP connector unavailable for r/%s (%s); using requests_backup.",
                clean_sub,
                mcp_exc,
            )
            backup_posts = self.fetch_via_requests_backup(clean_sub, limit=limit)
            return backup_posts, "requests_backup"

    def fetch_multiple_subreddits(
        self,
        subreddits: list[str],
        limit_per_sub: int = 8,
        seen_ids: set[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch newest posts across multiple subreddits efficiently using shared sessions."""
        seen_raw = {str(x).strip() for x in (seen_ids or set()) if str(x).strip()}
        seen_bare = {x.removeprefix("t3_") for x in seen_raw}

        all_posts: list[dict[str, Any]] = []
        new_posts: list[dict[str, Any]] = []
        by_subreddit: dict[str, list[dict[str, Any]]] = {}
        source_breakdown: dict[str, int] = {"mcp": 0, "requests_backup": 0, "failed": 0}
        errors: dict[str, str] = {}

        seen_in_this_batch: set[str] = set()
        mcp_available_for_batch = True
        std_requests_blocked_for_batch = False
        shared_cffi = self._get_shared_cffi_session()

        # Clean subreddit list while preserving order
        clean_subs: list[str] = []
        seen_subs_lower: set[str] = set()
        for raw_sub in subreddits:
            norm = normalize_subreddit_name(raw_sub)
            if norm and norm.lower() not in seen_subs_lower:
                seen_subs_lower.add(norm.lower())
                clean_subs.append(norm)

        for idx, sub in enumerate(clean_subs):
            posts: list[dict[str, Any]] = []
            used_source: str | None = None

            if mcp_available_for_batch:
                try:
                    posts = self._fetch_via_mcp(sub, limit=limit_per_sub, settings=settings)
                    used_source = "mcp"
                except Exception as mcp_exc:
                    if idx == 0 or isinstance(mcp_exc, McpConnectorError):
                        mcp_available_for_batch = False
                        logger.info(
                            "Marking MCP unavailable for remainder of batch after r/%s: %s",
                            sub,
                            mcp_exc,
                        )
                    else:
                        logger.info("MCP error on r/%s, falling back to backup: %s", sub, mcp_exc)

            if used_source is None:
                try:
                    if not std_requests_blocked_for_batch:
                        std_posts = self._try_standard_requests_json(sub, limit=limit_per_sub)
                        if std_posts is not None:
                            posts = std_posts
                            used_source = "requests_backup"
                        else:
                            std_requests_blocked_for_batch = True

                    if used_source is None:
                        posts = self._fetch_via_cffi_shreddit(
                            sub,
                            limit=limit_per_sub,
                            session=shared_cffi,
                        )
                        used_source = "requests_backup"
                except Exception as backup_exc:
                    logger.warning("Failed fetching r/%s via backup: %s", sub, backup_exc)
                    source_breakdown["failed"] += 1
                    by_subreddit[sub] = []
                    errors[sub] = str(backup_exc)
                    continue

            source_breakdown[used_source] += 1
            by_subreddit[sub] = posts

            for post in posts:
                pid = str(post.get("id") or "").strip()
                if not pid or pid in seen_in_this_batch:
                    continue
                seen_in_this_batch.add(pid)
                all_posts.append(post)

                pid_bare = pid.removeprefix("t3_")
                if pid not in seen_raw and pid_bare not in seen_bare:
                    new_posts.append(post)

        return {
            "all_posts": all_posts,
            "new_posts": new_posts,
            "by_subreddit": by_subreddit,
            "source_breakdown": source_breakdown,
            "errors": errors,
        }


reddit_fetcher = RedditFetcher()
