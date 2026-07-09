#!/usr/bin/env python3
"""
Fix titles & descriptions on already-uploaded YouTube Shorts.

For every clip in dashboard/schedule.json that has a videoId (i.e. is already
live on YouTube), this rewrites the video's metadata in place via
videos.update (part=snippet):

  - title:       schedule.json's AI-generated `title` for that clip.
  - description: the tease from schedule.json + a fresh 10-15 hashtag line
                  (generated with Claude, topical to the clip) + a
                  "Watch the full series" cross-link block listing every
                  uploaded clip from the same batch.

This never deletes or re-uploads anything — it only calls videos.update on
existing video IDs. Safe to re-run: clips already fixed (schedule.json
`crossLinked: true`, or a live description that already has the footer) are
skipped unless --force is passed.

Usage:
  venv/bin/python fix_youtube_titles.py                # fix every uploaded clip
  venv/bin/python fix_youtube_titles.py --dry-run       # preview only, no API writes
  venv/bin/python fix_youtube_titles.py --batch <id>    # limit to one batchId
  venv/bin/python fix_youtube_titles.py --limit 3       # process only N clips (testing)
  venv/bin/python fix_youtube_titles.py --force         # re-fix clips already marked done
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DASHBOARD_DIR = ROOT / "dashboard"
SCHEDULE_FILE = DASHBOARD_DIR / "schedule.json"
CREDENTIALS_FILE = DASHBOARD_DIR / "yt-credentials.json"
TOKENS_FILE = DASHBOARD_DIR / "yt-tokens.json"

YT_VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Same marker server.js's own cross-linking feature uses, so the two stay
# compatible and never double up the footer on a clip.
FOOTER_MARKER = "--- Watch the full series ---"

# Used only if Claude is unavailable/fails — keeps the script functional offline.
GENERIC_HASHTAGS = [
    "#Shorts", "#Viral", "#Trending", "#ForYou", "#FYP", "#MustWatch",
    "#Motivation", "#Mindset", "#Inspiration", "#LifeLessons", "#Wisdom",
    "#TrueStory", "#Interview", "#Success", "#Perspective",
]
_STOPWORDS = {"the", "a", "an", "of", "to", "how", "and", "is", "in", "on", "for", "your", "you"}


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


# ── JSON helpers ──────────────────────────────────────────────────────────

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


# ── OAuth ─────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    raw_creds = load_json(CREDENTIALS_FILE, None)
    if not raw_creds:
        sys.exit(f"No YouTube credentials at {CREDENTIALS_FILE}")
    creds = raw_creds.get("web") or raw_creds.get("installed") or raw_creds

    tokens = load_json(TOKENS_FILE, None)
    if not tokens or not tokens.get("refresh_token"):
        sys.exit(f"No refresh_token in {TOKENS_FILE} — reconnect YouTube in the dashboard first")

    resp = requests.post(OAUTH_TOKEN_URL, data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if not resp.ok:
        sys.exit(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:300]}")

    tokens.update(resp.json())  # merge — refresh response has no new refresh_token
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    return tokens["access_token"]


# ── YouTube Data API ──────────────────────────────────────────────────────

def yt_get_snippet(session: requests.Session, video_id: str) -> dict | None:
    r = session.get(YT_VIDEOS_API, params={"part": "snippet", "id": video_id}, timeout=30)
    r.raise_for_status()
    items = r.json().get("items") or []
    return items[0]["snippet"] if items else None


def yt_update_snippet(session: requests.Session, video_id: str, snippet: dict) -> None:
    r = session.put(
        YT_VIDEOS_API,
        params={"part": "snippet"},
        json={"id": video_id, "snippet": snippet},
        timeout=30,
    )
    r.raise_for_status()


# ── Hashtags ──────────────────────────────────────────────────────────────

def strip_hashtags(text: str) -> str:
    """Drop a trailing run of #hashtag tokens (e.g. the old lone '#Shorts')."""
    return re.sub(r"(?:\s*#\w+)+\s*$", "", text or "").strip()


def fallback_hashtags(batch_label: str) -> list[str]:
    words = re.findall(r"[A-Za-z]+", batch_label)
    topical = []
    for w in words:
        if len(w) < 3 or w.lower() in _STOPWORDS:
            continue
        tag = f"#{w[0].upper()}{w[1:]}"
        if tag not in topical:
            topical.append(tag)
    tags = ["#Shorts"] + [t for t in topical if t != "#Shorts"]
    for g in GENERIC_HASHTAGS:
        if len(tags) >= 15:
            break
        if g not in tags:
            tags.append(g)
    return tags[:15]


def ai_hashtags_for_batch(client, batch_label: str, clips: list[dict]) -> list[list[str]] | None:
    """One Claude call per batch. Returns a list of hashtag-lists aligned with `clips`, or None."""
    listing = "\n".join(f'{i + 1}. "{c["title"]}" — {c["description"]}' for i, c in enumerate(clips))
    prompt = (
        f'Source video series: "{batch_label}"\n\n{listing}\n\n'
        "For EACH numbered clip above, generate 10-15 topical, relevant YouTube hashtags "
        "for a Short based on its content. Always include #Shorts as one of them.\n"
        'Reply ONLY with a JSON array of arrays, same order and count as the clips: '
        '[["#Shorts","#..."], ...]'
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = re.sub(r"```(?:json)?|```", "", response.content[0].text).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list) or len(parsed) != len(clips):
        raise ValueError(f"expected {len(clips)} hashtag lists, got {len(parsed)}")
    return [[t if t.startswith("#") else f"#{t}" for t in tags] for tags in parsed]


def build_description(base_text: str, hashtags: list[str], series_lines: list[str]) -> str:
    hashtag_line = " ".join(dict.fromkeys(hashtags))  # de-dup, keep order
    parts = [base_text, hashtag_line]
    if series_lines:
        parts.append(f"{FOOTER_MARKER}\n" + "\n".join(series_lines))
    return "\n\n".join(p for p in parts if p)


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", help="Only fix clips with this batchId")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes, write nothing")
    parser.add_argument("--limit", type=int, help="Process at most N clips (for testing)")
    parser.add_argument("--force", action="store_true", help="Re-fix clips already marked crossLinked")
    args = parser.parse_args()

    schedule = load_json(SCHEDULE_FILE, {})
    if not schedule:
        sys.exit(f"No schedule found at {SCHEDULE_FILE}")

    by_batch: dict[str, list[tuple[str, dict]]] = {}
    for filename, entry in schedule.items():
        if not entry.get("videoId"):
            continue
        if args.batch and entry.get("batchId") != args.batch:
            continue
        by_batch.setdefault(entry.get("batchId", ""), []).append((filename, entry))

    total = sum(len(v) for v in by_batch.values())
    if not total:
        print("No uploaded clips with a videoId found — nothing to fix.")
        return
    print(f"Found {total} uploaded clip(s) across {len(by_batch)} batch(es).")

    session = None
    if not args.dry_run:
        token = get_access_token()
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {token}"})

    anthropic_client = None
    try:
        import anthropic
        anthropic_client = anthropic.Anthropic()
    except Exception as exc:
        print(f"  Note: Claude hashtag generation unavailable ({exc}) — using fallback hashtags")

    fixed = skipped = failed = processed = 0

    for batch_id, items in by_batch.items():
        items.sort(key=lambda kv: kv[1].get("clipIndex", 0))
        series_lines = [
            f"Clip {e.get('clipIndex', i + 1)}: https://www.youtube.com/shorts/{e['videoId']}"
            for i, (_, e) in enumerate(items)
        ]
        batch_label = batch_id.replace("_", " ").strip()

        pending = [(fn, e) for fn, e in items if args.force or not e.get("crossLinked")]
        if not pending:
            skipped += len(items)
            continue

        clips_for_ai = [
            {"title": e.get("title", ""), "description": strip_hashtags(e.get("description", ""))}
            for _, e in pending
        ]
        hashtag_sets = None
        if anthropic_client:
            try:
                hashtag_sets = ai_hashtags_for_batch(anthropic_client, batch_label, clips_for_ai)
            except Exception as exc:
                print(f"  [{batch_id}] Claude hashtag generation failed ({exc}) — using fallback")

        for i, (filename, entry) in enumerate(pending):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            video_id = entry["videoId"]
            title = (entry.get("title") or "").strip() or Path(filename).stem.replace("_", " ")
            base_text = strip_hashtags(entry.get("description", "")).strip()
            tags = hashtag_sets[i] if hashtag_sets else fallback_hashtags(batch_label)
            new_description = build_description(base_text, tags, series_lines)

            print(f"\n[{video_id}] {filename}")
            print(f"  title: {title}")
            print("  description:\n    " + new_description.replace("\n", "\n    "))

            if args.dry_run:
                continue

            try:
                live = yt_get_snippet(session, video_id)
            except requests.HTTPError as exc:
                print(f"  ! lookup failed: HTTP {exc.response.status_code} {exc.response.text[:200]}")
                failed += 1
                continue

            if not live:
                print("  ! not found on YouTube, skipping")
                failed += 1
                continue

            if FOOTER_MARKER in (live.get("description") or "") and live.get("title") == title:
                print("  = already fixed on YouTube, marking done")
                entry["crossLinked"] = True
                skipped += 1
                continue

            snippet = {
                "title": title,
                "description": new_description,
                "categoryId": live.get("categoryId", "22"),
            }
            if live.get("tags"):
                snippet["tags"] = live["tags"]
            if live.get("defaultLanguage"):
                snippet["defaultLanguage"] = live["defaultLanguage"]

            try:
                yt_update_snippet(session, video_id, snippet)
                entry["crossLinked"] = True
                fixed += 1
                print("  updated")
            except requests.HTTPError as exc:
                print(f"  ! update failed: HTTP {exc.response.status_code} {exc.response.text[:300]}")
                failed += 1

            time.sleep(0.3)  # stay well under burst limits

    if not args.dry_run:
        SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
        print(f"\nDone — updated {fixed}, skipped {skipped}, failed {failed} (of {total}).")
    else:
        print(f"\nDry run complete — {total} clip(s) previewed, no changes written.")


if __name__ == "__main__":
    main()
