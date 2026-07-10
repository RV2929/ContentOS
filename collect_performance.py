#!/usr/bin/env python3
"""
Daily performance collector — pulls views/likes/comments (and whatever else
each platform reports) for every posted clip across YouTube, Instagram, and
TikTok, and appends one JSONL row per (date, filename, platform) to
dashboard/performance.jsonl. Data collection only — no analysis, no UI.

YouTube:   videos.list?part=statistics via the existing yt-tokens-*.json
           OAuth tokens (stats are public data, so any connected account's
           token works regardless of which channel uploaded the video).
Instagram
/TikTok:   Buffer's GraphQL `posts(...)  { metrics }` query, resolving the
           live channel ID per service fresh on every run (channel IDs can
           and do change on reconnect — don't hardcode them). Only clips
           with a recorded bufferPostId/tiktokBufferPostId (set by
           server.js at post time) can be correlated to a Buffer post.

Usage:
  venv/bin/python collect_performance.py             # collect + append
  venv/bin/python collect_performance.py --dry-run    # print rows, write nothing
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DASHBOARD_DIR = ROOT / "dashboard"
SCHEDULE_FILE = DASHBOARD_DIR / "schedule.json"
CONFIG_FILE = DASHBOARD_DIR / "config.json"
CREDENTIALS_FILE = DASHBOARD_DIR / "yt-credentials.json"
PERFORMANCE_FILE = DASHBOARD_DIR / "performance.jsonl"

YT_VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


# ── .env ──────────────────────────────────────────────────────────────────

def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

import buffer_poster as bp  # noqa: E402 — needs .env loaded first for BUFFER_ACCESS_TOKEN


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ── YouTube ───────────────────────────────────────────────────────────────

def yt_get_access_token() -> str | None:
    """Try every connected YouTube account's refresh token, return the first that works."""
    raw_creds = load_json(CREDENTIALS_FILE, None)
    if not raw_creds:
        return None
    creds = raw_creds.get("web") or raw_creds.get("installed") or raw_creds

    config = load_json(CONFIG_FILE, {})
    accounts = [a for a in config.get("accounts", []) if a.get("platform") == "youtube"]

    for acct in accounts:
        tokens_file = DASHBOARD_DIR / f"yt-tokens-{acct['id']}.json"
        tokens = load_json(tokens_file, None)
        if not tokens or not tokens.get("refresh_token"):
            continue
        try:
            resp = requests.post(OAUTH_TOKEN_URL, data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            }, timeout=30)
            resp.raise_for_status()
            new_tokens = resp.json()
            tokens.update(new_tokens)
            tokens_file.write_text(json.dumps(tokens, indent=2))
            return new_tokens["access_token"]
        except Exception as exc:
            log(f"YouTube: token refresh failed for {acct.get('name', acct['id'])} ({exc})")
            continue
    return None


def yt_fetch_stats(access_token: str, video_ids: list[str]) -> dict[str, dict]:
    """Returns {videoId: statistics dict} for every ID YouTube still has data for."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        r = session.get(YT_VIDEOS_API, params={"part": "statistics", "id": ",".join(chunk)}, timeout=30)
        r.raise_for_status()
        for item in r.json().get("items", []):
            out[item["id"]] = item["statistics"]
    return out


# ── Buffer (Instagram / TikTok) ──────────────────────────────────────────

POSTS_QUERY = """
query GetPosts($input: PostsInput!, $first: Int, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node { id sentAt externalLink metrics { type value unit } } }
  }
}
"""

# Buffer's PostMetricType -> our field name. Unit-driven int/float casting
# happens in extract_metrics; this just picks which metrics we keep.
METRIC_KEYS = {
    "reactions": "likes",
    "comments": "comments",
    "views": "views",
    "reach": "reach",
    "shares": "shares",
    "saves": "saves",
    "follows": "follows",
    "engagementRate": "engagementRate",
}


def buffer_fetch_posts(service: str) -> dict[str, dict]:
    """Returns {postId: node} for every sent post on the currently-connected
    channel for `service` ('instagram' | 'tiktok'). Resolves the channel ID
    fresh each call — Buffer channel IDs change on reconnect."""
    orgs = bp.get_organizations()
    if not orgs:
        return {}
    org_id = orgs[0]["id"]
    channels = bp.get_channels_for_org(org_id)
    channel = next(
        (c for c in channels if not c.get("isDisconnected") and c.get("service", "").lower() == service),
        None,
    )
    if not channel:
        log(f"Buffer: no connected {service} channel found — skipping")
        return {}

    result: dict[str, dict] = {}
    after = None
    while True:
        variables = {
            "input": {"organizationId": org_id, "filter": {"channelIds": [channel["id"]], "status": ["sent"]}},
            "first": 50,
            "after": after,
        }
        data = bp.gql(POSTS_QUERY, variables)
        page = data.get("posts") or {}
        for edge in page.get("edges", []):
            node = edge["node"]
            result[node["id"]] = node
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return result


def extract_metrics(metric_list: list[dict] | None) -> dict:
    out = {}
    for m in metric_list or []:
        key = METRIC_KEYS.get(m.get("type"))
        if not key:
            continue
        value = m.get("value")
        if m.get("unit") == "count" and value is not None:
            value = int(value)
        out[key] = value
    return out


# ── JSONL dedupe ──────────────────────────────────────────────────────────

def existing_keys_for_date(date_str: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not PERFORMANCE_FILE.exists():
        return keys
    for line in PERFORMANCE_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") == date_str:
            keys.add((row.get("filename"), row.get("platform")))
    return keys


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print rows that would be written, write nothing")
    args = parser.parse_args()

    schedule = load_json(SCHEDULE_FILE, {})
    if not schedule:
        log("No schedule.json found — nothing to collect")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    collected_at = datetime.now().astimezone().isoformat()
    existing = existing_keys_for_date(today)

    rows = []
    counts = {"youtube": 0, "instagram": 0, "tiktok": 0}
    skipped = {"youtube_missing": 0, "instagram_no_id": 0, "instagram_no_metrics": 0,
               "tiktok_no_id": 0, "tiktok_no_metrics": 0}

    # ── YouTube ──
    yt_entries = [(fn, e) for fn, e in schedule.items()
                  if e.get("status") == "done" and e.get("videoId") and (fn, "youtube") not in existing]
    if yt_entries:
        access_token = yt_get_access_token()
        if not access_token:
            log("YouTube: no working access token for any connected account — skipping YouTube collection")
        else:
            try:
                yt_stats = yt_fetch_stats(access_token, [e["videoId"] for _, e in yt_entries])
            except Exception as exc:
                log(f"YouTube: stats fetch failed ({exc}) — skipping YouTube collection")
                yt_stats = {}
            for fn, e in yt_entries:
                stats = yt_stats.get(e["videoId"])
                if not stats:
                    skipped["youtube_missing"] += 1
                    continue
                rows.append({
                    "date": today,
                    "filename": fn,
                    "batchId": e.get("batchId"),
                    "channel": e.get("channel"),
                    "platform": "youtube",
                    "postId": e["videoId"],
                    "postedAt": e.get("scheduledAt"),
                    "externalLink": f"https://www.youtube.com/shorts/{e['videoId']}",
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "collectedAt": collected_at,
                })
                counts["youtube"] += 1

    # ── Instagram / TikTok (via Buffer) ──
    if not bp.BUFFER_TOKEN:
        log("Buffer: BUFFER_ACCESS_TOKEN not set — skipping Instagram/TikTok collection")
    else:
        for platform, status_key, id_key, no_id_key, no_metrics_key in [
            ("instagram", "bufferStatus", "bufferPostId", "instagram_no_id", "instagram_no_metrics"),
            ("tiktok", "tiktokBufferStatus", "tiktokBufferPostId", "tiktok_no_id", "tiktok_no_metrics"),
        ]:
            entries = [(fn, e) for fn, e in schedule.items()
                       if e.get(status_key) == "done" and (fn, platform) not in existing]
            if not entries:
                continue
            try:
                posts = buffer_fetch_posts(platform)
            except Exception as exc:
                log(f"Buffer: {platform} fetch failed ({exc}) — skipping {platform} collection")
                continue
            for fn, e in entries:
                post_id = e.get(id_key)
                if not post_id:
                    skipped[no_id_key] += 1
                    continue
                node = posts.get(post_id)
                if not node or node.get("metrics") is None:
                    skipped[no_metrics_key] += 1
                    continue
                rows.append({
                    "date": today,
                    "filename": fn,
                    "batchId": e.get("batchId"),
                    "channel": e.get("channel"),
                    "platform": platform,
                    "postId": post_id,
                    "postedAt": node.get("sentAt"),
                    "externalLink": node.get("externalLink"),
                    **extract_metrics(node.get("metrics")),
                    "collectedAt": collected_at,
                })
                counts[platform] += 1

    if args.dry_run:
        print(json.dumps(rows, indent=2))
    elif rows:
        with PERFORMANCE_FILE.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    log(
        f"Collected {sum(counts.values())} row(s) — "
        f"youtube={counts['youtube']} instagram={counts['instagram']} tiktok={counts['tiktok']} | "
        f"skipped — yt_missing={skipped['youtube_missing']} "
        f"ig_no_id={skipped['instagram_no_id']} ig_no_metrics={skipped['instagram_no_metrics']} "
        f"tt_no_id={skipped['tiktok_no_id']} tt_no_metrics={skipped['tiktok_no_metrics']}"
        + (" (dry-run, nothing written)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
