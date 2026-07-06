#!/usr/bin/env python3
"""
Post a ContentOS clip to Buffer for Instagram scheduling.

Usage:
  python buffer_poster.py /path/to/clip.mp4 "Caption #hashtags"
  python buffer_poster.py --profiles          (list connected Buffer profiles)

Output: JSON  {"ok": true, "updateId": "..."}
         or   {"ok": false, "error": "..."}
"""

import sys, os, json, argparse
from pathlib import Path

# Load .env from ContentOS root
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import requests

BUFFER_TOKEN      = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BUFFER_PROFILE_ID = os.environ.get("BUFFER_PROFILE_ID", "")
BASE_URL          = "https://api.bufferapp.com/1"


# ── API helpers ───────────────────────────────────────────────────────────────

def get_profiles() -> list:
    r = requests.get(
        f"{BASE_URL}/profiles.json",
        params={"access_token": BUFFER_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def find_instagram_profile(profiles: list) -> str | None:
    if BUFFER_PROFILE_ID:
        return BUFFER_PROFILE_ID
    for p in profiles:
        if p.get("service") in ("instagram", "instagrambusiness"):
            return p.get("id")
    return None


def upload_media(video_path: Path) -> str:
    """Upload the MP4 to Buffer and return the media id."""
    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"[buffer] Uploading {video_path.name} ({size_mb:.1f} MB)…", flush=True)
    with open(video_path, "rb") as fh:
        r = requests.post(
            f"{BASE_URL}/media/upload.json",
            data={"access_token": BUFFER_TOKEN},
            files={"file": (video_path.name, fh, "video/mp4")},
            timeout=300,
        )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Media upload rejected: {data}")
    media_id = data.get("media", {}).get("id") or data.get("id")
    if not media_id:
        raise RuntimeError(f"No media ID returned: {data}")
    return str(media_id)


def create_update(profile_id: str, text: str, media_id: str) -> dict:
    r = requests.post(
        f"{BASE_URL}/updates/create.json",
        data={
            "access_token": BUFFER_TOKEN,
            "profile_ids[]": profile_id,
            "text": text,
            "media[video]": media_id,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def post_video(video_path: str, caption: str) -> dict:
    if not BUFFER_TOKEN:
        return {"ok": False, "error": "BUFFER_ACCESS_TOKEN not set in .env"}

    vpath = Path(video_path)
    if not vpath.exists():
        return {"ok": False, "error": f"File not found: {vpath}"}

    try:
        print("[buffer] Fetching profiles…", flush=True)
        profiles = get_profiles()
        profile_id = find_instagram_profile(profiles)
        if not profile_id:
            return {
                "ok": False,
                "error": (
                    "No Instagram profile found in Buffer. "
                    "Connect one at buffer.com, or set BUFFER_PROFILE_ID= in .env."
                ),
            }
        print(f"[buffer] Profile: {profile_id}", flush=True)

        media_id = upload_media(vpath)
        print(f"[buffer] Media ID: {media_id}", flush=True)

        print("[buffer] Creating update…", flush=True)
        result = create_update(profile_id, caption, media_id)

        updates = result.get("updates") or []
        update_id = (updates[0].get("id") if updates else None) or result.get("id")
        print(f"[buffer] Queued! Update ID: {update_id}", flush=True)
        return {"ok": True, "updateId": update_id}

    except requests.HTTPError as exc:
        try:
            body = exc.response.json()
        except Exception:
            body = exc.response.text[:400]
        return {"ok": False, "error": f"Buffer API {exc.response.status_code}: {body}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post a clip to Buffer / Instagram")
    parser.add_argument("--profiles", action="store_true", help="List connected Buffer profiles and exit")
    parser.add_argument("video",   nargs="?", help="Path to MP4 file")
    parser.add_argument("caption", nargs="?", default="", help="Caption text with hashtags")
    args = parser.parse_args()

    if not BUFFER_TOKEN:
        print(json.dumps({"ok": False, "error": "BUFFER_ACCESS_TOKEN not set in .env"}))
        sys.exit(1)

    if args.profiles:
        try:
            for p in get_profiles():
                username = p.get("formatted_username") or p.get("service_username", "")
                print(f"  {p['id']}  {p['service']}  @{username}")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if not args.video:
        parser.print_help()
        sys.exit(1)

    cap = args.caption or (
        Path(args.video).stem.replace("_", " ") + "\n\n#Reels #Instagram #FYP #Viral"
    )
    result = post_video(args.video, cap)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
