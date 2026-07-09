"""
ContentOS — paste a YouTube URL, get viral vertical clips.

Pipeline:
  1. Download  (yt-dlp)
  2. Transcribe (WhisperX)
  3. Analyze   (Claude API)
  4. Clip      (FFmpeg — cut + 9:16 crop)
"""

import sys
import os
import time
import json
import datetime
import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent / "dashboard"

# ── Step checks ──────────────────────────────────────────────────────────────

def _check_api_key() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set.")
        print("  Run:  export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)


def _header(step: int, title: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  Step {step}: {title}")
    print(f"{'─' * 50}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(url: str, model_size: str = "base", channel: str = "podcast") -> list[str]:
    """
    Full pipeline: URL → list of output clip paths.

    Args:
        url:        YouTube URL
        model_size: WhisperX model ("tiny", "base", "small", "medium", "large-v2")
        channel:    Which channel this content belongs to ("podcast" or "football") —
                    tags the resulting schedule entries so uploads route to the right account
    """
    _check_api_key()
    total_start = time.time()

    # ── 1. Download ───────────────────────────────────────────────────────────
    _header(1, "Download")
    from downloader import download_video
    video_path = download_video(url)

    # ── 2. Transcribe ─────────────────────────────────────────────────────────
    _header(2, "Transcribe")
    from transcriber import transcribe
    transcript_path = transcribe(video_path, model_size=model_size)

    # ── 3. Analyze ────────────────────────────────────────────────────────────
    _header(3, "Find viral moments (Claude)")
    from analyzer import find_viral_clips, save_clips_json
    clips = find_viral_clips(transcript_path)

    if not clips:
        print("\nNo viral clips found. Try a longer video with more varied content.")
        return []

    clips_json_path = save_clips_json(clips, transcript_path)
    print(f"  → {len(clips)} clip(s) identified")

    # ── 4. Cut + crop + captions ──────────────────────────────────────────────
    _header(4, "Cut, crop to 9:16, burn captions")
    from clipper import process_clips
    output_paths = process_clips(video_path, clips_json_path, transcript_path, channel=channel)

    # ── 5. Generate titles & schedule uploads ────────────────────────────────
    if output_paths:
        _header(5, "Generate titles & schedule uploads")
        metadata = _generate_metadata(output_paths, clips, video_path)
        _schedule_clips(output_paths, clips, metadata, channel)

    # ── 6. Sync to GitHub so Vercel dashboard reflects new clips ─────────────
    if output_paths:
        _header(6, "Sync clip data to GitHub")
        _sync_to_github(f"{len(output_paths)} clip(s) from {os.path.basename(video_path)}")

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - total_start
    mins, secs = divmod(int(elapsed), 60)

    print(f"\n{'═' * 50}")
    print(f"  Done in {mins}m {secs}s — {len(output_paths)} clip(s) ready")
    print(f"{'═' * 50}")
    for path in output_paths:
        print(f"  {path}")

    return output_paths


def _sync_to_github(label: str = "update") -> None:
    """
    Write clips-data.json from current state/schedule/clips, then git push
    so the Vercel dashboard reads fresh data from GitHub.
    """
    try:
        clips_dir   = DASHBOARD_DIR.parent / "clips"
        state_file  = DASHBOARD_DIR / "state.json"
        sched_file  = DASHBOARD_DIR / "schedule.json"
        thumbs_dir  = DASHBOARD_DIR / "public" / "thumbnails"
        out_file    = DASHBOARD_DIR / "public" / "clips-data.json"

        state    = json.loads(state_file.read_text())  if state_file.exists()  else {}
        schedule = json.loads(sched_file.read_text())  if sched_file.exists()  else {}

        # Clips now live in clips/podcast/ and clips/football/ — fall back to
        # the flat clips/ dir too in case anything hasn't been migrated yet.
        mp4_paths = []
        for sub in ("podcast", "football", "."):
            d = clips_dir / sub if sub != "." else clips_dir
            if d.exists():
                mp4_paths.extend(d.glob("*.mp4"))
        seen_names = set()

        clips = []
        for mp4 in sorted(mp4_paths, key=lambda p: p.name):
            fn = mp4.name
            if fn in seen_names:
                continue
            seen_names.add(fn)
            stem = mp4.stem
            s    = state.get(fn, {})
            sch  = schedule.get(fn, {})
            pending = sch.get("status") in ("pending", "uploading")
            clips.append({
                "filename":      fn,
                "status":        s.get("status", "ready"),
                "youtubeId":     s.get("youtubeId", ""),
                "scheduledAt":   sch.get("scheduledAt") if pending else None,
                "scheduleStatus": sch.get("status") or None,
                "title":         sch.get("title", ""),
                "channel":       sch.get("channel") or s.get("channel") or "podcast",
                "thumbnailPath": f"/thumbnails/{stem}.jpg" if (thumbs_dir / f"{stem}.jpg").exists() else None,
            })

        out_file.write_text(json.dumps({
            "lastUpdated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clips": clips,
        }, indent=2))

        subprocess.run(
            ["git", "add", "public/clips-data.json", "public/thumbnails/"],
            cwd=str(DASHBOARD_DIR), capture_output=True,
        )
        r = subprocess.run(
            ["git", "commit", "-m", f"sync: {label}"],
            cwd=str(DASHBOARD_DIR), capture_output=True, text=True,
        )
        if "nothing to commit" not in r.stdout + r.stderr:
            subprocess.run(["git", "push"], cwd=str(DASHBOARD_DIR), capture_output=True)
            print(f"  → Synced to GitHub ({len(clips)} clips)")
    except Exception as exc:
        print(f"  ⚠ GitHub sync skipped: {exc}")


def _generate_metadata(
    output_paths: list[str], clips: list[dict], video_path: str
) -> dict[str, dict]:
    """
    Call Claude once to generate viral YouTube titles and descriptions for all clips.
    Returns {filename: {title, description}}.
    """
    import json
    import re
    import anthropic

    video_title = os.path.splitext(os.path.basename(video_path))[0].replace("_", " ")

    # Map each output path to its clip data via the numeric index in the filename (_clip01_, etc.)
    path_clip_pairs: list[tuple[str, dict]] = []
    for path in output_paths:
        filename = os.path.basename(path)
        m = re.search(r"_clip(\d+)_", filename)
        idx = int(m.group(1)) - 1 if m else None
        clip_data = clips[idx] if idx is not None and idx < len(clips) else {}
        path_clip_pairs.append((filename, clip_data))

    clips_block = "\n".join(
        f'Clip {i + 1}: Hook="{c.get("title", "")}" | Reason="{c.get("reason", "")}"'
        for i, (_, c) in enumerate(path_clip_pairs)
    )

    prompt = f"""You are a viral YouTube Shorts content strategist.

Source video: "{video_title}"

{clips_block}

For EACH clip generate:
- title: 60–100 chars, punchy, curiosity-driven, expert/authority framing when relevant. No emojis.
- description: 2 sentences that tease without spoiling. End with a line of ONLY hashtags: always include #Shorts plus 10–14 more topical hashtags relevant to the clip content (e.g. #AI #Tech #Future #Innovation #Psychology #Mindset #Motivation #Science #Health etc).

Reply ONLY with a valid JSON array in the same order as the clips:
[{{"title":"...","description":"..."}},...]"""

    fallback = [
        {
            "title": c.get("title", "") or os.path.splitext(fn)[0].replace("_", " ")[:80],
            "description": "#Shorts",
        }
        for fn, c in path_clip_pairs
    ]

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = re.sub(r"```(?:json)?|```", "", response.content[0].text).strip()
        metadata_list = json.loads(text)
    except Exception as exc:
        print(f"  Warning: metadata generation failed ({exc}) — using defaults")
        metadata_list = fallback

    result: dict[str, dict] = {}
    for (filename, _), meta in zip(path_clip_pairs, metadata_list):
        title = (meta.get("title") or "").strip()
        description = (meta.get("description") or "").strip()
        # Guarantee description ends with #Shorts
        if "#Shorts" not in description:
            description = (description + " #Shorts").strip()
        result[filename] = {"title": title, "description": description}

    print(f"  Generated metadata for {len(result)} clip(s):")
    for meta in result.values():
        print(f"    {meta['title'][:72]}")

    return result


def _schedule_clips(
    output_paths: list[str], clips: list[dict], metadata: dict[str, dict], channel: str = "podcast"
) -> None:
    """Schedule clips every 2.5 h, storing generated titles/descriptions and batchId."""
    import datetime
    import json
    import re

    INTERVAL_HOURS = 2.5

    schedule_file = os.path.join(os.path.dirname(__file__), "dashboard", "schedule.json")
    try:
        with open(schedule_file, "r", encoding="utf-8") as f:
            schedule = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        schedule = {}

    if not output_paths:
        return

    # Stable batch ID derived from the video stem of the first clip
    first_name = os.path.basename(output_paths[0])
    m_batch = re.match(r"^(.+?)_clip\d+", first_name)
    batch_id = m_batch.group(1) if m_batch else os.path.splitext(first_name)[0][:50]

    start = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)

    newly_scheduled: list[tuple[str, datetime.datetime, int]] = []
    for i, clip_path in enumerate(output_paths):
        filename = os.path.basename(clip_path)
        if schedule.get(filename, {}).get("status") in ("uploading", "done"):
            continue

        m_idx = re.search(r"_clip(\d+)_", filename)
        clip_index = int(m_idx.group(1)) if m_idx else (i + 1)

        clip_data = clips[clip_index - 1] if 0 <= clip_index - 1 < len(clips) else {}
        meta = metadata.get(filename, {})
        title = meta.get("title") or clip_data.get("title", "") or os.path.splitext(filename)[0].replace("_", " ")[:80]
        description = meta.get("description") or "#Shorts"

        scheduled_at = start + datetime.timedelta(hours=i * INTERVAL_HOURS)
        schedule[filename] = {
            "scheduledAt": scheduled_at.isoformat(),
            "title": title,
            "description": description,
            "visibility": "public",
            "status": "pending",
            "bufferStatus": "pending",
            "batchId": batch_id,
            "clipIndex": clip_index,
            "channel": channel,
        }
        newly_scheduled.append((filename, scheduled_at, clip_index))

    if not newly_scheduled:
        print("  No new clips to schedule.")
        return

    try:
        with open(schedule_file, "w", encoding="utf-8") as f:
            json.dump(schedule, f, indent=2)
    except OSError as exc:
        print(f"  Warning: could not write schedule — {exc}")
        return

    print(f"  {len(newly_scheduled)} clip(s) queued every {INTERVAL_HOURS}h (batch: {batch_id}):")
    for filename, at, idx in sorted(newly_scheduled, key=lambda x: x[2]):
        local_at = at.astimezone()
        print(f"  Clip {idx:02d}: {local_at.strftime('%b %d %I:%M %p')}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _usage() -> None:
    print("Usage: python contentos.py <youtube_url> [options]")
    print()
    print("Options:")
    print("  --model   <size>   WhisperX model: tiny, base, small, medium, large-v2")
    print("                     (default: base — faster; use small/medium for accuracy)")
    print()
    print("Example:")
    print("  python contentos.py 'https://youtu.be/dQw4w9WgXcQ' --model small")
    print()
    print("Claude automatically decides how many clips to extract (3–10),")
    print("only pulling moments that genuinely have viral potential.")
    print()
    print("Requires: ANTHROPIC_API_KEY environment variable")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _usage()
        sys.exit(0)

    url = args[0]
    model_size = "base"
    channel = "podcast"

    i = 1
    while i < len(args):
        flag = args[i]
        if flag == "--model" and i + 1 < len(args):
            model_size = args[i + 1]
            i += 2
        elif flag == "--channel" and i + 1 < len(args):
            channel = args[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {flag}")
            _usage()
            sys.exit(1)

    run(url, model_size=model_size, channel=channel)
