#!/usr/bin/env python3
"""
Post a ContentOS clip to Buffer (Instagram) via Buffer's GraphQL API.
Endpoint: https://api.buffer.com
Auth:     Authorization: Bearer <BUFFER_ACCESS_TOKEN>

Buffer's VideoAssetInput requires a public URL — it fetches the video itself.
Set VIDEO_BASE_URL in .env to your Cloudflare tunnel URL. The ContentOS server
already serves clips at /clips/:filename, so Buffer can reach any clip at
VIDEO_BASE_URL/clips/filename.mp4.

Usage:
  python buffer_poster.py /path/to/clip.mp4 "Caption #hashtags"
  python buffer_poster.py --profiles   list organizations
  python buffer_poster.py --channels   list all connected social channels
  python buffer_poster.py --schema     print available GraphQL mutations

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
# musichub29_ — TikTok is podcast-only, so this channel never receives football clips.
BUFFER_TIKTOK_CHANNEL_ID = os.environ.get("BUFFER_TIKTOK_CHANNEL_ID", "6a4f7c9b4048344628886484")
GQL_URL           = "https://api.buffer.com"


# ── GraphQL client ────────────────────────────────────────────────────────────

def gql(query: str, variables: dict = None, timeout: int = 30) -> dict:
    """Execute a GraphQL query/mutation. Returns the data field or raises."""
    headers = {
        "Authorization": f"Bearer {BUFFER_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(GQL_URL, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
        raise RuntimeError(f"GraphQL error: {msgs}")
    return body.get("data") or {}


# ── Channels ──────────────────────────────────────────────────────────────────

ORGANIZATIONS_QUERY = """
query GetOrganizations { account { organizations { id } } }
"""

# channels() requires input: ChannelsInput! { organizationId: OrganizationId! }
ORG_CHANNELS_QUERY = """
query GetOrgChannels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    displayName
    service
    type
    descriptor
    isDisconnected
    serviceId
    avatar
    externalLink
  }
}
"""

def get_organizations() -> list:
    data = gql(ORGANIZATIONS_QUERY)
    return (data.get("account") or {}).get("organizations") or []


def get_channels_for_org(org_id: str) -> list:
    return gql(ORG_CHANNELS_QUERY, {"input": {"organizationId": org_id}}).get("channels") or []


def get_channels() -> list:
    """Fetch channels for the first available organization."""
    orgs = get_organizations()
    if not orgs:
        return []
    return get_channels_for_org(orgs[0]["id"])


def find_instagram_channel(channels: list) -> str | None:
    if BUFFER_PROFILE_ID:
        return BUFFER_PROFILE_ID
    for ch in channels:
        if not ch.get("isDisconnected") and "instagram" in ch.get("service", "").lower():
            return ch.get("id")
    return None


def find_tiktok_channel(channels: list) -> str | None:
    if BUFFER_TIKTOK_CHANNEL_ID:
        return BUFFER_TIKTOK_CHANNEL_ID
    for ch in channels:
        if not ch.get("isDisconnected") and "tiktok" in ch.get("service", "").lower():
            return ch.get("id")
    return None


# ── Video URL ─────────────────────────────────────────────────────────────────
# Buffer's VideoAssetInput requires a public URL — it fetches the file itself.
# The ContentOS server serves clips at /clips/:filename.
#
# The tunnel doesn't use a domain, so its URL changes every time it restarts.
# run-tunnel.sh keeps tunnel-url.txt updated with whatever URL is currently
# live — read that first so posts never use a stale address. VIDEO_BASE_URL
# in .env is kept as a fallback (also auto-updated by run-tunnel.sh).

VIDEO_BASE_URL = os.environ.get("VIDEO_BASE_URL", "").rstrip("/")

_tunnel_url_file = Path(__file__).parent / "tunnel-url.txt"
if _tunnel_url_file.exists():
    _live_url = _tunnel_url_file.read_text().strip()
    if _live_url:
        VIDEO_BASE_URL = _live_url.rstrip("/")


def get_public_video_url(video_path: Path) -> str:
    if not VIDEO_BASE_URL:
        raise RuntimeError(
            "VIDEO_BASE_URL is not set in .env. "
            "Add your Cloudflare tunnel URL, e.g. VIDEO_BASE_URL=https://xxxx.trycloudflare.com"
        )
    url = f"{VIDEO_BASE_URL}/clips/{video_path.name}"
    print(f"[buffer] Video URL: {url}", flush=True)
    return url


# ── Post creation ─────────────────────────────────────────────────────────────
# createPost returns PostActionPayload (union) — must use inline fragments.
# schedulingType: automatic  = Buffer publishes natively via API (vs "notification",
#                              which just pings the user's phone to post manually)
# mode: shareNow             = Buffer fetches the asset and publishes immediately.
#                              We deliberately don't use "addToQueue": our own
#                              scheduler (server.js) already decides the exact
#                              post time, and addToQueue defers Buffer's own
#                              media fetch to whenever it reaches that queue
#                              slot — by which point our Cloudflare quick-tunnel
#                              URL may have rotated, causing late, silent
#                              "issue with the media attached" failures.
#                              shareNow fetches the video right away, while the
#                              tunnel URL we just generated is still live.

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id status dueAt }
    }
    ... on InvalidInputError { message }
    ... on UnauthorizedError { message }
    ... on LimitReachedError { message }
    ... on NotFoundError     { message }
    ... on UnexpectedError   { message }
    ... on RestProxyError    { message }
  }
}
"""

def create_post(channel_id: str, text: str, video_url: str, platform: str = "instagram") -> str:
    """Create a Buffer post with a video URL and return the post ID."""
    input_data = {
        "channelId": channel_id,
        "text": text,
        "schedulingType": "automatic",
        "mode": "shareNow",
        "assets": [{"video": {"url": video_url}}],
    }
    # TikTokPostMetadataInput has no required fields (title, isAiGenerated are
    # both optional) — Buffer applies the account's default privacy/sharing
    # settings, so metadata can be omitted entirely for TikTok.
    if platform == "instagram":
        input_data["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    variables = {"input": input_data}
    data = gql(CREATE_POST_MUTATION, variables)
    result = data.get("createPost") or {}
    # Success
    if "post" in result:
        post_id = result["post"].get("id")
        if post_id:
            return post_id
    # Error variants all have a message field
    msg = result.get("message") or result.get("type") or "unknown error"
    raise RuntimeError(f"createPost failed: {msg} — full response: {result}")


# ── Introspection (for debugging schema) ─────────────────────────────────────

MUTATIONS_QUERY = """
query {
  __schema {
    mutationType {
      fields {
        name
        description
      }
    }
  }
}
"""


# ── Main flow ─────────────────────────────────────────────────────────────────

def build_caption(title: str) -> str:
    clean = " ".join(w for w in title.split() if not w.startswith("#"))
    return f"{clean}\n\n#Reels #Instagram #FYP #Viral"


def post_video(video_path: str, caption: str, platform: str = "instagram") -> dict:
    if not BUFFER_TOKEN:
        return {"ok": False, "error": "BUFFER_ACCESS_TOKEN not set in .env"}

    vpath = Path(video_path)
    if not vpath.exists():
        return {"ok": False, "error": f"File not found: {vpath}"}

    try:
        print(f"[buffer:{platform}] Fetching channels…", flush=True)
        channels = get_channels()
        channel_id = find_tiktok_channel(channels) if platform == "tiktok" else find_instagram_channel(channels)
        if not channel_id:
            names = [(ch.get("service", "?"), ch.get("name", "?")) for ch in channels]
            return {
                "ok": False,
                "error": (
                    f"No {platform.capitalize()} channel found in Buffer. "
                    "Connect one at buffer.com, or set "
                    f"{'BUFFER_TIKTOK_CHANNEL_ID' if platform == 'tiktok' else 'BUFFER_PROFILE_ID'}= in .env. "
                    f"Available channels: {names}"
                ),
            }
        print(f"[buffer:{platform}] Channel: {channel_id}", flush=True)

        video_url = get_public_video_url(vpath)

        print(f"[buffer:{platform}] Creating post…", flush=True)
        post_id = create_post(channel_id, caption, video_url, platform=platform)
        print(f"[buffer:{platform}] Post queued! ID: {post_id}", flush=True)
        return {"ok": True, "updateId": post_id}

    except requests.HTTPError as exc:
        try:
            body = exc.response.json()
        except Exception:
            body = exc.response.text[:400]
        return {"ok": False, "error": f"HTTP {exc.response.status_code}: {body}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Post a clip to Buffer / Instagram (GraphQL API)"
    )
    parser.add_argument("--profiles", action="store_true", help="List Buffer organizations")
    parser.add_argument("--channels", action="store_true", help="List all connected social channels")
    parser.add_argument("--schema",   action="store_true", help="Print available GraphQL mutations")
    parser.add_argument("--platform", choices=["instagram", "tiktok"], default="instagram",
                         help="Which connected channel to post to (default: instagram)")
    parser.add_argument("video",   nargs="?", help="Path to MP4 file")
    parser.add_argument("caption", nargs="?", default="", help="Caption with hashtags")
    args = parser.parse_args()

    if not BUFFER_TOKEN:
        print(json.dumps({"ok": False, "error": "BUFFER_ACCESS_TOKEN not set in .env"}))
        sys.exit(1)

    if args.schema:
        try:
            data = gql(MUTATIONS_QUERY)
            fields = data.get("__schema", {}).get("mutationType", {}).get("fields", [])
            print("Available GraphQL mutations:")
            for f in fields:
                desc = f.get("description") or ""
                print(f"  {f['name']}" + (f"  — {desc}" if desc else ""))
        except Exception as exc:
            print(f"Introspection error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.profiles:
        try:
            orgs = get_organizations()
            if not orgs:
                print("  No organizations found.")
            for org in orgs:
                print(f"  {org.get('id', '?')}")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.channels:
        try:
            orgs = get_organizations()
            if not orgs:
                print("  No organizations found.")
                sys.exit(0)
            for org in orgs:
                org_id = org.get("id", "?")
                print(f"Org: {org_id}")
                channels = get_channels_for_org(org_id)
                if not channels:
                    print("  No channels found.")
                    continue
                for ch in channels:
                    cid        = ch.get("id", "?")
                    service    = ch.get("service") or "?"
                    descriptor = ch.get("descriptor") or service
                    name       = ch.get("name") or ch.get("displayName") or "?"
                    link       = ch.get("externalLink") or ""
                    conn       = "✗ disconnected" if ch.get("isDisconnected") else "✓ connected"
                    print(f"  {conn:<16}  {descriptor:<28}  {name:<25}  {cid}")
                    if link:
                        print(f"    {link}")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if not args.video:
        parser.print_help()
        sys.exit(1)

    cap = args.caption or build_caption(Path(args.video).stem.replace("_", " "))
    result = post_video(args.video, cap, platform=args.platform)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
