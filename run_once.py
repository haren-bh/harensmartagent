"""CLI utility to trigger a single HarenSmartAgent Reddit monitoring run."""

from __future__ import annotations

import argparse
import json
import sys

import config  # noqa: F401 - ensures Vertex AI / mTLS env defaults are set
from services.runner_service import runner_service


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run HarenSmartAgent once across all monitored subreddits."
    )
    parser.add_argument(
        "--ignore-seen",
        action="store_true",
        help="Summarize recent posts even if their IDs were seen in a prior run.",
    )
    args = parser.parse_args()

    result = runner_service.execute_run(trigger="cli", ignore_seen=args.ignore_seen)
    summary = {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "started_at": result.get("started_at"),
        "subreddits_checked": result.get("subreddits_checked"),
        "total_posts_fetched": result.get("total_posts_fetched"),
        "new_posts_count": result.get("new_posts_count"),
        "source_breakdown": result.get("source_breakdown"),
        "discord_sent": result.get("discord_sent"),
        "discord_status": result.get("discord_status"),
        "gcs_blob_path": result.get("gcs_blob_path"),
        "short_summary": result.get("short_summary"),
    }
    print(json.dumps(summary, indent=2))
    return 0 if result.get("status") in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
