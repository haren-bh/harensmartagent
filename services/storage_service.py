"""Google Cloud Storage + local fallback persistence service for HarenSmartAgent."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import storage

try:
    from config import Config, DEFAULT_SUBREDDITS
except ImportError:  # pragma: no cover - package-relative import fallback
    from ..config import Config, DEFAULT_SUBREDDITS  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

MAX_SEEN_POST_IDS = 5000

RUN_SUMMARY_KEYS: tuple[str, ...] = (
    "run_id",
    "started_at",
    "completed_at",
    "trigger",
    "status",
    "source_breakdown",
    "subreddits_checked",
    "total_posts_fetched",
    "new_posts_count",
    "short_summary",
    "full_response",
    "discord_sent",
    "discord_status",
    "gcs_blob_path",
    "error",
)


def normalize_subreddit_name(raw: str) -> str:
    """Normalize a subreddit name or Reddit URL down to the bare subreddit name."""
    if not isinstance(raw, str):
        raw = str(raw or "")
    cleaned = raw.strip()
    if not cleaned:
        return ""

    # Strip query strings and fragments
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0].strip()

    # Extract from full or partial Reddit URLs like https://www.reddit.com/r/foo/new/
    url_match = re.search(r"(?:reddit\.com)?/+r/+([A-Za-z0-9_]+)", cleaned, flags=re.IGNORECASE)
    if url_match:
        return url_match.group(1)

    # Strip leading r/ or /r/ and trailing slashes
    cleaned = re.sub(r"^/+", "", cleaned)
    if cleaned.lower().startswith("r/"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/").strip()

    # Keep only the subreddit segment if a subpath like "foo/new" was given
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[0].strip()

    # Validate reasonable Reddit subreddit characters
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
    return cleaned


def normalize_subreddit_list(raw_items: Any) -> list[str]:
    """Normalize and deduplicate a list (or delimited string) of subreddits."""
    if isinstance(raw_items, str):
        tokens = re.split(r"[\s,;]+", raw_items)
    elif isinstance(raw_items, (list, tuple, set)):
        tokens = [str(item) for item in raw_items]
    else:
        tokens = []

    result: list[str] = []
    seen_lower: set[str] = set()
    for token in tokens:
        norm = normalize_subreddit_name(token)
        if norm and norm.lower() not in seen_lower:
            seen_lower.add(norm.lower())
            result.append(norm)
    return result


def _parse_iso_sort_key(ts: Any) -> tuple[float, str]:
    """Parse an ISO-8601 timestamp into a chronological sort tuple (epoch_seconds, raw_str)."""
    raw = str(ts or "").strip()
    if not raw:
        return (0.0, "")
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.timestamp(), raw)
    except ValueError:
        return (0.0, raw)


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


class StorageService:
    """Persists settings, seen post IDs, run records, and run index to GCS with local mirror."""

    def __init__(
        self,
        bucket_name: str | None = None,
        prefix: str | None = None,
        local_fallback_dir: Path | None = None,
    ) -> None:
        self.bucket_name: str = (bucket_name or Config.GCS_BUCKET_NAME).strip()
        self.prefix: str = (prefix or Config.GCS_PREFIX).strip().strip("/")
        self.local_dir: Path = Path(local_fallback_dir or Config.LOCAL_FALLBACK_DIR)
        self.local_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._client: storage.Client | None = None

    # ------------------------------------------------------------------
    # Internal GCS + local mirror helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> storage.Client:
        if self._client is None:
            self._client = storage.Client()
        return self._client

    def _blob_name(self, subpath: str) -> str:
        clean_sub = subpath.strip("/")
        return f"{self.prefix}/{clean_sub}" if self.prefix else clean_sub

    def _local_path(self, blob_name: str) -> Path:
        target = self.local_dir / blob_name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _write_local_json(self, blob_name: str, data: Any) -> None:
        path = self._local_path(blob_name)
        serialized = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            tmp_path.write_text(serialized, encoding="utf-8")
            tmp_path.replace(path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            path.write_text(serialized, encoding="utf-8")

    def _read_local_json(self, blob_name: str) -> tuple[bool, Any]:
        path = self._local_path(blob_name)
        if not path.exists():
            return False, None
        try:
            return True, json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed reading local fallback %s: %s", path, exc)
            return False, None

    def _write_json(self, subpath: str, data: Any) -> tuple[bool, str | None]:
        """Write JSON to local mirror first, then upload to GCS."""
        blob_name = self._blob_name(subpath)
        with self._lock:
            self._write_local_json(blob_name, data)
            serialized = json.dumps(data, indent=2, ensure_ascii=False)
            try:
                client = self._get_client()
                bucket = client.bucket(self.bucket_name)
                blob = bucket.blob(blob_name)
                blob.upload_from_string(serialized, content_type="application/json", timeout=15)
                return True, None
            except Exception as exc:
                logger.warning("GCS upload failed for gs://%s/%s (saved locally): %s", self.bucket_name, blob_name, exc)
                return False, str(exc)

    def _read_json(self, subpath: str, default: Any = None) -> Any:
        """Read JSON from GCS (updating local mirror) with automatic local fallback."""
        blob_name = self._blob_name(subpath)
        with self._lock:
            try:
                client = self._get_client()
                bucket = client.bucket(self.bucket_name)
                blob = bucket.blob(blob_name)
                raw_text = blob.download_as_text(encoding="utf-8", timeout=12)
                parsed = json.loads(raw_text)
                self._write_local_json(blob_name, parsed)
                return parsed
            except NotFound:
                found_local, local_data = self._read_local_json(blob_name)
                return local_data if found_local else default
            except Exception as exc:
                logger.warning("GCS read failed for gs://%s/%s, falling back to local mirror: %s", self.bucket_name, blob_name, exc)
                found_local, local_data = self._read_local_json(blob_name)
                return local_data if found_local else default

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def storage_health(self) -> dict[str, Any]:
        """Verify GCS bucket connectivity and existence cleanly."""
        try:
            client = self._get_client()
            bucket = client.bucket(self.bucket_name)
            bucket.reload(timeout=10)
            return {
                "bucket": self.bucket_name,
                "prefix": self.prefix,
                "connected": True,
                "error": None,
            }
        except NotFound:
            return {
                "bucket": self.bucket_name,
                "prefix": self.prefix,
                "connected": False,
                "error": f"GCS bucket '{self.bucket_name}' was not found.",
            }
        except (GoogleAPIError, Exception) as exc:
            return {
                "bucket": self.bucket_name,
                "prefix": self.prefix,
                "connected": False,
                "error": str(exc),
            }

    def _default_settings(self) -> dict[str, Any]:
        return {
            "subreddits": list(DEFAULT_SUBREDDITS),
            "posts_per_subreddit": int(Config.POSTS_PER_SUBREDDIT),
            "schedule_interval_minutes": int(Config.SCHEDULE_INTERVAL_MINUTES),
            "scheduler_enabled": bool(Config.SCHEDULER_AUTO_START),
            "discord_webhook_url": str(Config.DISCORD_WEBHOOK_URL or ""),
            "discord_bot_token": str(Config.DISCORD_BOT_TOKEN or ""),
            "discord_channel_id": str(Config.DISCORD_CHANNEL_ID or ""),
            "discord_user_id": str(Config.DISCORD_USER_ID or ""),
            "reddit_client_id": str(Config.REDDIT_CLIENT_ID or ""),
            "reddit_client_secret": str(Config.REDDIT_CLIENT_SECRET or ""),
        }

    def get_settings(self) -> dict[str, Any]:
        """Read runtime settings merged onto defaults from Config and DEFAULT_SUBREDDITS."""
        defaults = self._default_settings()
        stored = self._read_json("config/runtime_settings.json", default=None)
        if not isinstance(stored, dict):
            return defaults

        merged = dict(defaults)
        for key in defaults:
            if key in stored and stored[key] is not None:
                merged[key] = stored[key]

        # Normalize types & subreddits
        norm_subs = normalize_subreddit_list(merged.get("subreddits"))
        merged["subreddits"] = norm_subs if norm_subs else list(DEFAULT_SUBREDDITS)

        try:
            merged["posts_per_subreddit"] = max(1, int(merged["posts_per_subreddit"]))
        except (ValueError, TypeError):
            merged["posts_per_subreddit"] = defaults["posts_per_subreddit"]

        try:
            merged["schedule_interval_minutes"] = max(1, int(merged["schedule_interval_minutes"]))
        except (ValueError, TypeError):
            merged["schedule_interval_minutes"] = defaults["schedule_interval_minutes"]

        merged["scheduler_enabled"] = _to_bool(
            merged.get("scheduler_enabled"), default=defaults["scheduler_enabled"]
        )

        for str_key in (
            "discord_webhook_url",
            "discord_bot_token",
            "discord_channel_id",
            "discord_user_id",
            "reddit_client_id",
            "reddit_client_secret",
        ):
            merged[str_key] = str(merged.get(str_key) or "").strip()

        return merged

    def save_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge updates, normalize subreddit names, persist to GCS + local, and return merged dict."""
        with self._lock:
            current = self.get_settings()
            merged = dict(current)

            if isinstance(updates, dict):
                for key in current:
                    if key in updates and updates[key] is not None:
                        merged[key] = updates[key]

            if "subreddits" in (updates or {}):
                norm_subs = normalize_subreddit_list(updates["subreddits"])
                merged["subreddits"] = norm_subs if norm_subs else list(DEFAULT_SUBREDDITS)
            else:
                merged["subreddits"] = normalize_subreddit_list(merged["subreddits"]) or list(
                    DEFAULT_SUBREDDITS
                )

            try:
                merged["posts_per_subreddit"] = max(1, min(100, int(merged["posts_per_subreddit"])))
            except (ValueError, TypeError):
                merged["posts_per_subreddit"] = current["posts_per_subreddit"]

            try:
                merged["schedule_interval_minutes"] = max(
                    1, int(merged["schedule_interval_minutes"])
                )
            except (ValueError, TypeError):
                merged["schedule_interval_minutes"] = current["schedule_interval_minutes"]

            merged["scheduler_enabled"] = _to_bool(
                merged.get("scheduler_enabled"), default=current["scheduler_enabled"]
            )

            for str_key in (
                "discord_webhook_url",
                "discord_bot_token",
                "discord_channel_id",
                "discord_user_id",
                "reddit_client_id",
                "reddit_client_secret",
            ):
                merged[str_key] = str(merged.get(str_key) or "").strip()

            self._write_json("config/runtime_settings.json", merged)
            return merged

    # ------------------------------------------------------------------
    # Seen Reddit Post IDs State
    # ------------------------------------------------------------------

    def _read_seen_post_list(self) -> list[str]:
        raw = self._read_json("state/seen_posts.json", default=None)
        if isinstance(raw, dict):
            items = raw.get("seen_ids", [])
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        return [str(x).strip() for x in items if str(x).strip()]

    def get_seen_post_ids(self) -> set[str]:
        """Return the set of previously seen Reddit post IDs."""
        return set(self._read_seen_post_list())

    def save_seen_post_ids(self, seen_ids: set[str] | list[str]) -> None:
        """Persist seen post IDs capped at the latest 5000 IDs."""
        with self._lock:
            if isinstance(seen_ids, list):
                ordered: list[str] = []
                seen_track: set[str] = set()
                for item in seen_ids:
                    s = str(item).strip()
                    if s and s not in seen_track:
                        seen_track.add(s)
                        ordered.append(s)
            else:
                existing = self._read_seen_post_list()
                incoming_set = {str(x).strip() for x in seen_ids if str(x).strip()}
                # Keep existing order for IDs still in incoming_set, then append brand new IDs
                ordered = [x for x in existing if x in incoming_set]
                already = set(ordered)
                for item in sorted(incoming_set):
                    if item not in already:
                        already.add(item)
                        ordered.append(item)

            capped = ordered[-MAX_SEEN_POST_IDS:]
            payload = {
                "seen_ids": capped,
                "count": len(capped),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_json("state/seen_posts.json", payload)

    def clear_seen_post_ids(self) -> None:
        """Clear all stored seen Reddit post IDs."""
        with self._lock:
            payload = {
                "seen_ids": [],
                "count": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_json("state/seen_posts.json", payload)

    # ------------------------------------------------------------------
    # Run History & Index
    # ------------------------------------------------------------------

    @staticmethod
    def _build_compact_summary(run_record: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in RUN_SUMMARY_KEYS:
            if key in run_record:
                summary[key] = run_record[key]
            elif key == "source_breakdown":
                summary[key] = {"mcp": 0, "requests_backup": 0, "failed": 0}
            elif key == "subreddits_checked":
                summary[key] = []
            elif key in ("total_posts_fetched", "new_posts_count"):
                summary[key] = 0
            elif key == "discord_sent":
                summary[key] = False
            elif key in ("short_summary", "full_response", "discord_status", "gcs_blob_path"):
                summary[key] = ""
            else:
                summary[key] = None
        return summary

    def save_run(self, run_record: dict[str, Any]) -> dict[str, Any]:
        """Save full run JSON to {prefix}/runs/{safe_ts}_{run_id}.json and update runs_index.json."""
        with self._lock:
            record = dict(run_record)
            run_id = str(record.get("run_id") or uuid.uuid4().hex[:12]).strip()
            started_at = str(
                record.get("started_at") or datetime.now(timezone.utc).isoformat()
            ).strip()
            record["run_id"] = run_id
            record["started_at"] = started_at

            safe_ts = re.sub(r"[^0-9A-Za-zT_-]", "-", started_at)
            rel_subpath = f"runs/{safe_ts}_{run_id}.json"
            blob_name = self._blob_name(rel_subpath)
            record["gcs_blob_path"] = f"gs://{self.bucket_name}/{blob_name}"

            # 1. Persist full run record
            self._write_json(rel_subpath, record)

            # 2. Update compact index in {prefix}/index/runs_index.json
            compact = self._build_compact_summary(record)
            existing_index_raw = self._read_json("index/runs_index.json", default=None)
            if isinstance(existing_index_raw, dict):
                runs_list = list(existing_index_raw.get("runs", []))
            elif isinstance(existing_index_raw, list):
                runs_list = list(existing_index_raw)
            else:
                runs_list = []

            updated_list: list[dict[str, Any]] = []
            replaced = False
            for item in runs_list:
                if isinstance(item, dict) and item.get("run_id") == run_id:
                    updated_list.append(compact)
                    replaced = True
                elif isinstance(item, dict):
                    updated_list.append(item)

            if not replaced:
                updated_list.append(compact)

            updated_list.sort(key=lambda r: _parse_iso_sort_key(r.get("started_at")))
            self._write_json("index/runs_index.json", updated_list)

            return record

    def _rebuild_runs_from_blobs(self) -> list[dict[str, Any]]:
        """Fallback scan of {prefix}/runs/*.json if runs_index.json does not exist yet."""
        by_run_id: dict[str, dict[str, Any]] = {}
        runs_prefix = f"{self._blob_name('runs')}/"

        # Try listing from GCS
        try:
            client = self._get_client()
            blobs = client.list_blobs(self.bucket_name, prefix=runs_prefix, timeout=15)
            for blob in blobs:
                if not blob.name.endswith(".json"):
                    continue
                try:
                    data = json.loads(blob.download_as_text(encoding="utf-8", timeout=10))
                    if isinstance(data, dict) and data.get("run_id"):
                        self._write_local_json(blob.name, data)
                        by_run_id[str(data["run_id"])] = self._build_compact_summary(data)
                except Exception as exc:
                    logger.warning("Skipping unreadable run blob %s: %s", blob.name, exc)
        except Exception as exc:
            logger.warning("GCS blob listing unavailable, scanning local fallback runs: %s", exc)

        # Merge any local fallback run files
        local_runs_dir = self._local_path(self._blob_name("runs"))
        if local_runs_dir.exists() and local_runs_dir.is_dir():
            for path in local_runs_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("run_id"):
                        rid = str(data["run_id"])
                        if rid not in by_run_id:
                            by_run_id[rid] = self._build_compact_summary(data)
                except Exception:
                    continue

        summaries = list(by_run_id.values())
        summaries.sort(key=lambda r: _parse_iso_sort_key(r.get("started_at")))
        if summaries:
            self._write_json("index/runs_index.json", summaries)
        return summaries

    def list_runs(self, order: str = "asc", limit: int = 200) -> list[dict[str, Any]]:
        """Return run summaries sorted chronologically by started_at (default asc: oldest-to-newest)."""
        raw_index = self._read_json("index/runs_index.json", default=None)
        if raw_index is None:
            runs = self._rebuild_runs_from_blobs()
        elif isinstance(raw_index, dict):
            runs = [r for r in raw_index.get("runs", []) if isinstance(r, dict)]
        elif isinstance(raw_index, list):
            runs = [r for r in raw_index if isinstance(r, dict)]
        else:
            runs = []

        is_desc = str(order or "asc").strip().lower() == "desc"
        sorted_runs = sorted(
            runs,
            key=lambda r: _parse_iso_sort_key(r.get("started_at")),
            reverse=is_desc,
        )
        if limit and limit > 0:
            return sorted_runs[:limit]
        return sorted_runs

    def _subpath_from_gcs_uri(self, gcs_uri: str) -> str | None:
        expected_prefix = f"gs://{self.bucket_name}/"
        if not gcs_uri or not gcs_uri.startswith(expected_prefix):
            return None
        blob_name = gcs_uri[len(expected_prefix) :]
        own_prefix = f"{self.prefix}/" if self.prefix else ""
        if own_prefix and blob_name.startswith(own_prefix):
            return blob_name[len(own_prefix) :]
        return blob_name

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Retrieve the full run record for a specific run_id."""
        target_id = str(run_id or "").strip()
        if not target_id:
            return None

        # 1. Fast path via index gcs_blob_path
        raw_index = self._read_json("index/runs_index.json", default=None)
        entries: list[dict[str, Any]] = []
        if isinstance(raw_index, list):
            entries = [r for r in raw_index if isinstance(r, dict)]
        elif isinstance(raw_index, dict):
            entries = [r for r in raw_index.get("runs", []) if isinstance(r, dict)]

        for entry in entries:
            if entry.get("run_id") == target_id:
                subpath = self._subpath_from_gcs_uri(str(entry.get("gcs_blob_path") or ""))
                if subpath:
                    full_record = self._read_json(subpath, default=None)
                    if isinstance(full_record, dict):
                        return full_record

        # 2. Fallback scan in local mirror
        local_runs_dir = self._local_path(self._blob_name("runs"))
        if local_runs_dir.exists() and local_runs_dir.is_dir():
            for candidate in local_runs_dir.glob(f"*_{target_id}.json"):
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("run_id") == target_id:
                        return data
                except Exception:
                    continue

        # 3. Fallback scan in GCS runs/ prefix
        runs_prefix = f"{self._blob_name('runs')}/"
        suffix = f"_{target_id}.json"
        try:
            client = self._get_client()
            for blob in client.list_blobs(self.bucket_name, prefix=runs_prefix, timeout=15):
                if blob.name.endswith(suffix):
                    data = json.loads(blob.download_as_text(encoding="utf-8", timeout=12))
                    if isinstance(data, dict):
                        self._write_local_json(blob.name, data)
                        return data
        except Exception as exc:
            logger.warning("GCS lookup failed for run_id=%s: %s", target_id, exc)

        return None


storage_service = StorageService()
