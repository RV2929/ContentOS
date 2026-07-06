#!/usr/bin/env python3
"""
Post a ContentOS clip to Buffer (Instagram) via Buffer's GraphQL API.
Endpoint: https://api.buffer.com
Auth:     Authorization: Bearer <BUFFER_ACCESS_TOKEN>

Buffer's VideoAssetInput requires a public URL — it fetches the file itself.
If VIDEO_BASE_URL is set in .env (e.g. a Cloudflare/ngrok tunnel pointing
at the ContentOS server), clip URLs are built from that. Otherwise the file
is uploaded to transfer.sh to get a temporary public URL.

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


# ── Video hosting ─────────────────────────────────────────────────────────────
# Buffer's VideoAssetInput only accepts a public URL — it fetches the file itself.
# If VIDEO_BASE_URL is set (e.g. a Cloudflare Tunnel exposing the ContentOS server),
# the clip URL is constructed directly. Otherwise the file is pushed to transfer.sh.

VIDEO_BASE_URL = os.environ.get("VIDEO_BASE_URL", "").rstrip("/")
CLIPS_DIR      = Path(__file__).parent / "clips"


def get_public_video_url(video_path: Path) -> str:
    if VIDEO_BASE_URL:
        url = f"{VIDEO_BASE_URL}/clips/{video_path.name}"
        print(f"[buffer] Using public URL: {url}", flush=True)
        return url

    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"[buffer] Uploading {video_path.name} ({size_mb:.1f} MB) to transfer.sh…", flush=True)
    with open(video_path, "rb") as fh:
        r = requests.put(
            f"https://transfer.sh/{video_path.name}",
            data=fh,
            headers={"Content-Type": "video/mp4", "Max-Days": "3"},
            timeout=600,
        )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Unexpected transfer.sh response: {url[:200]}")
    print(f"[buffer] Public URL: {url}", flush=True)
    return url


# ── Post creation ─────────────────────────────────────────────────────────────
# createPost returns PostActionPayload (union) — must use inline fragments.
# schedulingType: automatic  = Buffer publishes automatically at the due time
# mode: addToQueue           = slot into the channel's posting schedule

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id status dueAt }
    }
    ... on InvalidInputError { message type }
    ... on UnauthorizedError { message type }
    ... on LimitReachedError { message type }
    ... on NotFoundError     { message type }
    ... on UnexpectedError   { message type }
    ... on RestProxyError    { message type }
  }
}
"""

def create_post(channel_id: str, text: str, video_url: str) -> str:
    """Create a Buffer post with a video URL and return the post ID."""
    variables = {
        "input": {
            "channelId": channel_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": "addToQueue",
            "assets": [{"video": {"url": video_url}}],
        }
    }
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


def post_video(video_path: str, caption: str) -> dict:
    if not BUFFER_TOKEN:
        return {"ok": False, "error": "BUFFER_ACCESS_TOKEN not set in .env"}

    vpath = Path(video_path)
    if not vpath.exists():
        return {"ok": False, "error": f"File not found: {vpath}"}

    try:
        print("[buffer] Fetching channels…", flush=True)
        channels = get_channels()
        channel_id = find_instagram_channel(channels)
        if not channel_id:
            names = [(ch.get("service", "?"), ch.get("name", "?")) for ch in channels]
            return {
                "ok": False,
                "error": (
                    "No Instagram channel found in Buffer. "
                    "Connect one at buffer.com, or set BUFFER_PROFILE_ID= in .env. "
                    f"Available channels: {names}"
                ),
            }
        print(f"[buffer] Channel: {channel_id}", flush=True)

        video_url = get_public_video_url(vpath)

        print("[buffer] Creating post…", flush=True)
        post_id = create_post(channel_id, caption, video_url)
        print(f"[buffer] Post queued! ID: {post_id}", flush=True)
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
    result = post_video(args.video, cap)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
