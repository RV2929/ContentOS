#!/usr/bin/env python3
"""
Post a ContentOS clip to Buffer (Instagram) via Buffer's GraphQL API.
Endpoint: https://api.bufferapp.com/graphql
Auth:     Authorization: Bearer <BUFFER_ACCESS_TOKEN>

Usage:
  python buffer_poster.py /path/to/clip.mp4 "Caption #hashtags"
  python buffer_poster.py --profiles   list connected channels
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
GQL_URL           = "https://api.bufferapp.com/graphql"


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

CHANNELS_QUERY = """
query GetChannels {
  channels {
    id
    name
    service
    serviceType
    avatar
    isConnected
  }
}
"""

def get_channels() -> list:
    return gql(CHANNELS_QUERY).get("channels") or []


def find_instagram_channel(channels: list) -> str | None:
    if BUFFER_PROFILE_ID:
        return BUFFER_PROFILE_ID
    for ch in channels:
        svc = (ch.get("service") or ch.get("serviceType") or "").lower()
        if "instagram" in svc:
            return ch.get("id")
    return None


# ── Media upload ──────────────────────────────────────────────────────────────

REQUEST_UPLOAD_MUTATION = """
mutation RequestUpload($input: RequestUploadInput!) {
  requestUpload(input: $input) {
    uploadId
    uri
    method
    headers {
      name
      value
    }
    maxFileSize
  }
}
"""

def request_upload_url(media_type: str = "video/mp4") -> tuple:
    """Ask Buffer for a signed upload URL. Returns (uploadId, uri, method, extra_headers)."""
    data = gql(REQUEST_UPLOAD_MUTATION, {"input": {"type": media_type}})
    upload = data.get("requestUpload") or {}
    upload_id  = upload.get("uploadId")
    uri        = upload.get("uri")
    method     = (upload.get("method") or "PUT").upper()
    raw_hdrs   = upload.get("headers") or []
    extra_hdrs = {h["name"]: h["value"] for h in raw_hdrs}
    if not upload_id or not uri:
        raise RuntimeError(f"No upload URL returned from requestUpload: {upload}")
    return upload_id, uri, method, extra_hdrs


def upload_to_signed_url(uri: str, method: str, extra_headers: dict, video_path: Path) -> None:
    """PUT (or POST) the video bytes to the signed storage URL."""
    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"[buffer] Uploading to storage ({size_mb:.1f} MB, {method})…", flush=True)
    headers = {"Content-Type": "video/mp4", **extra_headers}
    with open(video_path, "rb") as fh:
        fn = getattr(requests, method.lower(), requests.put)
        r = fn(uri, data=fh, headers=headers, timeout=600)
    r.raise_for_status()
    print(f"[buffer] Upload complete (HTTP {r.status_code})", flush=True)


# ── Post creation ─────────────────────────────────────────────────────────────

CREATE_POST_MUTATION = """
mutation CreatePost($input: PostCreateInput!) {
  postCreate(input: $input) {
    post {
      id
      status
      dueAt
    }
    errors {
      type
      message
    }
  }
}
"""

def create_post(channel_id: str, text: str, upload_id: str) -> str:
    """Create a Buffer post and return the post ID."""
    variables = {
        "input": {
            "channelId": channel_id,
            "text": text,
            "media": [{"uploadId": upload_id, "type": "VIDEO"}],
        }
    }
    data = gql(CREATE_POST_MUTATION, variables)
    result = data.get("postCreate") or {}
    errors = result.get("errors") or []
    if errors:
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        raise RuntimeError(f"postCreate error: {msgs}")
    post = result.get("post") or {}
    post_id = post.get("id")
    if not post_id:
        raise RuntimeError(f"No post ID returned: {result}")
    return post_id


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

        print("[buffer] Requesting upload URL…", flush=True)
        upload_id, uri, method, extra_headers = request_upload_url("video/mp4")
        print(f"[buffer] Upload ID: {upload_id}", flush=True)

        upload_to_signed_url(uri, method, extra_headers, vpath)

        print("[buffer] Creating post…", flush=True)
        post_id = create_post(channel_id, caption, upload_id)
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
    parser.add_argument("--profiles", action="store_true", help="List connected Buffer channels")
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
            channels = get_channels()
            if not channels:
                print("  No channels found.")
            for ch in channels:
                svc  = ch.get("service") or ch.get("serviceType") or "?"
                name = ch.get("name") or "?"
                cid  = ch.get("id") or "?"
                conn = "✓" if ch.get("isConnected") else "✗"
                print(f"  {conn} {cid}  {svc}  {name}")
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
