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
import shutil
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent / "dashboard"

# Curated viral hashtags per channel niche — always guaranteed present in every
# clip's description, alongside the AI-generated topical tags and speaker hashtag.
CHANNEL_HASHTAGS = {
    "podcast":   ["#podcast", "#podcastclips", "#interview", "#viral", "#fyp"],
    "football":  ["#football", "#soccer", "#footballclips", "#viral", "#fyp"],
    "streamers": ["#kick", "#streamer", "#gaming", "#twitch", "#viral", "#fyp"],
}

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
        channel:    Which channel this content belongs to ("podcast", "football", or "streamers") —
                    tags the resulting schedule entries so uploads route to the right account
    """
    _check_api_key()
    total_start = time.time()

    # ── 1. Download ───────────────────────────────────────────────────────────
    _header(1, "Download")
    from downloader import download_video
    video_path, video_meta = download_video(url)

    # ── 2. Transcribe ─────────────────────────────────────────────────────────
    _header(2, "Transcribe")
    from transcriber import transcribe
    transcript_path = transcribe(video_path, model_size=model_size)

    # ── 3. Analyze ────────────────────────────────────────────────────────────
    _header(3, "Find viral moments (Claude)")
    from analyzer import (
        find_viral_clips, find_tiktok_clips, save_clips_json, save_tiktok_clips_json,
    )
    clips = find_viral_clips(transcript_path)

    # TikTok's Creator Rewards Program requires 60+ second videos, so those clips
    # need their own ~62s pass with a strong early hook — run alongside (not
    # instead of) the short-clip search above. TikTok posting is podcast-only
    # today (see BUFFER_TIKTOK_CHANNEL_ID in buffer_poster.py), so this pass
    # only runs for that channel.
    tiktok_clips: list[dict] = []
    if channel == "podcast":
        print("  Finding ~62s TikTok moments…")
        tiktok_clips = find_tiktok_clips(transcript_path)
        if tiktok_clips:
            print(f"  → {len(tiktok_clips)} TikTok clip(s) identified")

            # Original spoken commentary intro — helps clear TikTok's Creator
            # Rewards "adds new ideas" bar on top of the reused source footage.
            print("  Generating commentary intros…")
            from commentary import add_commentary
            tiktok_clips = add_commentary(tiktok_clips)
        else:
            print("  → no segment could sustain a full ~62s TikTok clip")

    if not clips and not tiktok_clips:
        print("\nNo viral clips found. Try a longer video with more varied content.")
        return []

    output_paths: list[str] = []
    tiktok_output_paths: list[str] = []

    from clipper import process_clips

    if clips:
        clips_json_path = save_clips_json(clips, transcript_path)

        # ── 4. Cut + crop + captions ──────────────────────────────────────────
        _header(4, "Cut, crop to 9:16, burn captions")
        output_paths = process_clips(video_path, clips_json_path, transcript_path, channel=channel)

    if tiktok_clips:
        tiktok_clips_json_path = save_tiktok_clips_json(tiktok_clips, transcript_path)

        _header(4, "Cut, crop to 9:16, burn captions (TikTok ~62s)")
        tiktok_output_paths = process_clips(
            video_path, tiktok_clips_json_path, transcript_path, channel=channel, clip_label="tiktok",
        )

    # ── 5. Generate titles & schedule uploads ────────────────────────────────
    if output_paths or tiktok_output_paths:
        _header(5, "Generate titles & schedule uploads")

        if output_paths:
            metadata = _generate_metadata(output_paths, clips, video_path, video_meta, channel)
            _schedule_clips(output_paths, clips, metadata, channel)

        if tiktok_output_paths:
            tiktok_metadata = _generate_metadata(
                tiktok_output_paths, tiktok_clips, video_path, video_meta, channel,
                clip_label="tiktok", platform="tiktok",
            )
            tiktok_schedule_file = os.path.join(os.path.dirname(__file__), "dashboard", "tiktok_schedule.json")
            _schedule_clips(
                tiktok_output_paths, tiktok_clips, tiktok_metadata, channel,
                clip_label="tiktok", schedule_path=tiktok_schedule_file,
            )

    all_output_paths = output_paths + tiktok_output_paths

    # ── 6. Sync to GitHub so Vercel dashboard reflects new clips ─────────────
    if all_output_paths:
        _header(6, "Sync clip data to GitHub")
        _sync_to_github(f"{len(all_output_paths)} clip(s) from {os.path.basename(video_path)}")

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - total_start
    mins, secs = divmod(int(elapsed), 60)

    print(f"\n{'═' * 50}")
    print(f"  Done in {mins}m {secs}s — {len(all_output_paths)} clip(s) ready")
    print(f"{'═' * 50}")
    for path in output_paths:
        print(f"  {path}")
    for path in tiktok_output_paths:
        print(f"  {path}  (TikTok, ~62s)")

    return all_output_paths


def _sync_to_github(label: str = "update") -> None:
    """
    Write clips-data.json from current state/schedule/clips, then git push
    so the Vercel dashboard reads fresh data from GitHub.
    """
    try:
        clips_dir     = DASHBOARD_DIR.parent / "clips"
        state_file    = DASHBOARD_DIR / "state.json"
        sched_file    = DASHBOARD_DIR / "schedule.json"
        tt_sched_file = DASHBOARD_DIR / "tiktok_schedule.json"
        thumbs_dir    = DASHBOARD_DIR / "public" / "thumbnails"
        out_file      = DASHBOARD_DIR / "public" / "clips-data.json"

        state         = json.loads(state_file.read_text())     if state_file.exists()     else {}
        schedule      = json.loads(sched_file.read_text())     if sched_file.exists()     else {}
        tiktok_schedule = json.loads(tt_sched_file.read_text()) if tt_sched_file.exists() else {}

        # Clips now live in clips/podcast/, clips/football/, and clips/streamers/
        # — fall back to the flat clips/ dir too in case anything hasn't been
        # migrated yet.
        mp4_paths = []
        for sub in ("podcast", "football", "streamers", "."):
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
            sch  = schedule.get(fn) or tiktok_schedule.get(fn) or {}
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
    output_paths: list[str], clips: list[dict], video_path: str, video_meta: dict, channel: str,
    clip_label: str = "clip", platform: str = "youtube",
) -> dict[str, dict]:
    """
    Call Claude once to generate viral titles and descriptions for all clips.
    Returns {filename: {title, description}}.

    clip_label must match whatever process_clips() was called with, so the
    numeric index embedded in each filename maps back to the right clip data.
    platform="tiktok" skips the #Shorts hashtag (YouTube-specific).
    """
    import json
    import re
    import anthropic
    from scheduler_queue import normalize_channel

    # Prefer the real YouTube title over the filesystem-sanitized filename when available.
    video_title = video_meta.get("title") or os.path.splitext(os.path.basename(video_path))[0].replace("_", " ")
    video_uploader = video_meta.get("uploader") or ""
    # Truncate — descriptions can run to thousands of chars (links, sponsor blocks,
    # timestamps); the guest/speaker is almost always named in the first paragraph.
    video_description = (video_meta.get("description") or "")[:500]

    channel_hashtags = CHANNEL_HASHTAGS[normalize_channel(channel)]
    channel_hashtags_str = " ".join(channel_hashtags)

    # Map each output path to its clip data via the numeric index in the filename (_clip01_, etc.)
    path_clip_pairs: list[tuple[str, dict]] = []
    for path in output_paths:
        filename = os.path.basename(path)
        m = re.search(rf"_{re.escape(clip_label)}(\d+)_", filename)
        idx = int(m.group(1)) - 1 if m else None
        clip_data = clips[idx] if idx is not None and idx < len(clips) else {}
        path_clip_pairs.append((filename, clip_data))

    clips_block = "\n".join(
        f'Clip {i + 1}: Hook="{c.get("title", "")}" | Reason="{c.get("reason", "")}"'
        for i, (_, c) in enumerate(path_clip_pairs)
    )

    leading_tag = "" if platform == "tiktok" else "#Shorts, then "
    strategist_line = (
        "You are a viral TikTok content strategist."
        if platform == "tiktok"
        else "You are a viral YouTube Shorts content strategist."
    )

    prompt = f"""{strategist_line}

Source video: "{video_title}"
Uploader/Channel: "{video_uploader}"
Description: "{video_description}"

{clips_block}

For EACH clip generate:
- title: 60–100 chars, punchy, curiosity-driven, expert/authority framing when relevant. No emojis.
- description: 2 sentences that tease without spoiling. End with a line of ONLY hashtags, in this order: {leading_tag}these required channel hashtags: {channel_hashtags_str}, then 6–10 more topical hashtags relevant to the clip content (e.g. #AI #Tech #Future #Innovation #Psychology #Mindset #Motivation #Science #Health etc) — don't repeat the channel hashtags{' or #Shorts' if leading_tag else ''} among the topical ones.
- speaker_hashtag: a single lowercase hashtag for the video's main speaker/guest (e.g. "#taylorswift"), identified from the uploader/description/title above — not the channel's own brand name unless the channel IS the speaker. No spaces or punctuation besides the leading #. If no individual speaker can be confidently identified, return "".

Reply ONLY with a valid JSON array in the same order as the clips:
[{{"title":"...","description":"...","speaker_hashtag":"..."}},...]"""

    base_tags = channel_hashtags if platform == "tiktok" else ["#Shorts"] + channel_hashtags
    fallback = [
        {
            "title": c.get("title", "") or os.path.splitext(fn)[0].replace("_", " ")[:80],
            "description": " ".join(base_tags),
            "speaker_hashtag": "",
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
        # Normalize whatever Claude returned into a clean #hashtag token (strip spaces/punctuation).
        speaker_tag = re.sub(r"[^a-zA-Z0-9]", "", meta.get("speaker_hashtag") or "")
        speaker_hashtag = f"#{speaker_tag}" if speaker_tag else ""

        # Guarantee #Shorts (YouTube only), the channel's curated hashtags, and the
        # speaker hashtag are always present — Claude usually includes them, but this
        # makes it deterministic rather than dependent on instruction-following.
        required_tags = base_tags + ([speaker_hashtag] if speaker_hashtag else [])
        description_lower = description.lower()
        for tag in required_tags:
            if tag.lower() not in description_lower:
                description = (description + " " + tag).strip()
                description_lower = description.lower()

        # Whatever's left after stripping #Shorts/channel/speaker tags is the
        # topical hashtags Claude generated for this clip — surface them
        # separately so Instagram/TikTok captions (which don't reuse the full
        # YouTube description) can pull a few in alongside their own tags.
        exclude = {t.lower() for t in required_tags}
        topical_hashtags = [
            t for t in dict.fromkeys(re.findall(r"#\w+", description))
            if t.lower() not in exclude
        ]

        result[filename] = {
            "title": title,
            "description": description,
            "speaker_hashtag": speaker_hashtag,
            "topical_hashtags": topical_hashtags,
        }

    print(f"  Generated metadata for {len(result)} clip(s):")
    for meta in result.values():
        print(f"    {meta['title'][:72]}")

    return result


def _schedule_clips(
    output_paths: list[str], clips: list[dict], metadata: dict[str, dict], channel: str = "podcast",
    clip_label: str = "clip", schedule_path: str | None = None,
) -> None:
    """Queue clips onto a per-channel daily posting schedule (see
    scheduler_queue.py): up to DAILY_CAP per calendar day per channel,
    spread across the posting window, FIFO after whatever's already queued.

    schedule_path defaults to dashboard/schedule.json (short clips → YouTube +
    Instagram). Pass dashboard/tiktok_schedule.json to queue the ~62s TikTok
    clips instead — a separate file means they get their own independent
    daily cap/slot pool rather than competing with the short clips for slots,
    and the dashboard server's TikTok Buffer loop reads from that file only.
    clip_label must match whatever process_clips() was called with, so the
    numeric index embedded in each filename maps back to the right clip data.
    """
    import json
    import re

    from scheduler_queue import DAILY_CAP, allocate_slots, normalize_channel

    channel = normalize_channel(channel)

    schedule_file = schedule_path or os.path.join(os.path.dirname(__file__), "dashboard", "schedule.json")
    try:
        with open(schedule_file, "r", encoding="utf-8") as f:
            schedule = json.load(f)
    except FileNotFoundError:
        schedule = {}
    except json.JSONDecodeError as exc:
        # File exists but won't parse — could be a mid-write race with the
        # dashboard server (fs.writeFileSync there isn't atomic). Never treat
        # this the same as "file doesn't exist yet": silently falling back to
        # {} here would mean the json.dump() below overwrites the whole file
        # with just this run's clips, wiping out everything already queued.
        corrupt_copy = f"{schedule_file}.corrupt"
        shutil.copy(schedule_file, corrupt_copy)
        raise RuntimeError(
            f"{schedule_file} exists but is not valid JSON ({exc}) — refusing to overwrite it. "
            f"Unreadable copy saved to {corrupt_copy} for inspection."
        ) from exc

    if not output_paths:
        return

    # Stable batch ID derived from the video stem of the first clip
    first_name = os.path.basename(output_paths[0])
    m_batch = re.match(rf"^(.+?)_{re.escape(clip_label)}\d+", first_name)
    batch_id = m_batch.group(1) if m_batch else os.path.splitext(first_name)[0][:50]

    pending_filenames: list[str] = []
    pending_meta: dict[str, dict] = {}
    for i, clip_path in enumerate(output_paths):
        filename = os.path.basename(clip_path)
        if schedule.get(filename, {}).get("status") in ("uploading", "done"):
            continue

        m_idx = re.search(rf"_{re.escape(clip_label)}(\d+)_", filename)
        clip_index = int(m_idx.group(1)) if m_idx else (i + 1)

        clip_data = clips[clip_index - 1] if 0 <= clip_index - 1 < len(clips) else {}
        meta = metadata.get(filename, {})
        title = meta.get("title") or clip_data.get("title", "") or os.path.splitext(filename)[0].replace("_", " ")[:80]
        description = meta.get("description") or ("" if clip_label == "tiktok" else "#Shorts")

        pending_filenames.append(filename)
        pending_meta[filename] = {
            "title": title,
            "description": description,
            "clip_index": clip_index,
            "speaker_hashtag": meta.get("speaker_hashtag", ""),
            "topical_hashtags": meta.get("topical_hashtags", []),
        }

    if not pending_filenames:
        print("  No new clips to schedule.")
        return

    slots = allocate_slots(schedule, channel, pending_filenames)

    newly_scheduled: list[tuple[str, datetime.datetime, int]] = []
    for filename in pending_filenames:
        scheduled_at = slots[filename]
        meta = pending_meta[filename]
        schedule[filename] = {
            "scheduledAt": scheduled_at.isoformat(),
            "title": meta["title"],
            "description": meta["description"],
            "speakerHashtag": meta["speaker_hashtag"],
            "topicalHashtags": meta["topical_hashtags"],
            "visibility": "public",
            "status": "pending",
            "bufferStatus": "pending",
            "batchId": batch_id,
            "clipIndex": meta["clip_index"],
            "channel": channel,
        }
        newly_scheduled.append((filename, scheduled_at, meta["clip_index"]))

    try:
        with open(schedule_file, "w", encoding="utf-8") as f:
            json.dump(schedule, f, indent=2)
    except OSError as exc:
        print(f"  Warning: could not write schedule — {exc}")
        return

    print(f"  {len(newly_scheduled)} clip(s) queued (max {DAILY_CAP}/day/channel, batch: {batch_id}):")
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
