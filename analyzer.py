"""
Step 3 — Analyze a transcript with Claude and identify viral clip candidates.
Reads a .transcript.json file and returns a list of {start, end, reason} dicts.
"""

import sys
import os
import json
import anthropic

CLIPS_MIN_DURATION = 15   # seconds — ignore candidate clips shorter than this
CLIPS_MAX_DURATION = 90   # seconds — cap candidates at this length


def _build_transcript_text(transcript: dict) -> str:
    """Flatten WhisperX segments into a timestamped plain-text block for Claude."""
    lines = []
    for seg in transcript.get("segments", []):
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"[{start:.2f}s – {end:.2f}s] {text}")
    return "\n".join(lines)


def _total_duration(transcript: dict) -> float:
    segs = transcript.get("segments", [])
    if not segs:
        return 0.0
    return segs[-1].get("end", 0.0)


def _excerpt_for_range(transcript: dict, start: float, end: float) -> str:
    """Flatten transcript segments overlapping [start, end] into a plain-text excerpt.

    Used to ground commentary generation in what was actually said in a specific
    clip, rather than just its title/reason.
    """
    lines = []
    for seg in transcript.get("segments", []):
        s = seg.get("start", 0)
        e = seg.get("end", 0)
        text = seg.get("text", "").strip()
        if text and e > start and s < end:
            lines.append(text)
    return " ".join(lines)


CLIPS_MIN_COUNT = 3
CLIPS_MAX_COUNT = 10

# TikTok Creator Rewards Program requires videos 60+ seconds long — target ~62s
# to clear that bar with a little headroom, with a strong hook up front to
# retain viewers through the extra runtime.
TIKTOK_TARGET_DURATION = 62   # seconds
TIKTOK_MIN_DURATION = 58      # seconds — floor of the acceptable range
TIKTOK_MAX_DURATION = 68      # seconds — ceiling of the acceptable range
TIKTOK_HOOK_MAX_START = 15    # hook must land within the first 5-15s of the clip
TIKTOK_MAX_COUNT = 5          # soft cap — Claude returns fewer (even zero) if the content isn't there


def find_viral_clips(transcript_path: str) -> list[dict]:
    """
    Send the transcript to Claude and get back viral clip candidates.
    Claude decides how many clips to return (between CLIPS_MIN_COUNT and CLIPS_MAX_COUNT),
    only including moments that genuinely have viral potential.

    Returns a list of dicts:
        [{"start": float, "end": float, "reason": str}, ...]
    """
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    transcript_text = _build_transcript_text(transcript)
    total_dur = _total_duration(transcript)

    if not transcript_text.strip():
        print("⚠ Transcript appears empty — no clips to find.")
        return []

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    system_prompt = (
        "You are a senior short-form video editor who has grown multiple accounts to millions "
        "of followers on TikTok, Instagram Reels, and YouTube Shorts. You have a sharp eye for "
        "what makes people stop scrolling: unexpected insights, raw emotion, a story with stakes, "
        "a counterintuitive claim, a moment of genuine humour, or a line so quotable it gets "
        "screenshotted. You are ruthlessly selective — you would rather return three killer clips "
        "than ten mediocre ones."
    )

    user_prompt = f"""Below is a word-level transcript from a video. Total duration: {total_dur:.1f}s.

---
{transcript_text}
---

Your task: identify every moment in this video that has genuine viral potential as a short-form vertical clip (9:16).

Criteria for inclusion (a clip must meet AT LEAST ONE):
- Opens with an irresistible hook that makes you need to keep watching
- Contains a surprising, counterintuitive, or little-known insight
- Has strong raw emotion — joy, anger, vulnerability, or awe
- Tells a tight story with a clear setup and payoff
- Delivers a line so quotable or provocative it would get screenshotted or shared
- Contains humour that lands naturally (not forced)

Rules:
- Each clip must be {CLIPS_MIN_DURATION}–{CLIPS_MAX_DURATION} seconds long.
- Timestamps must be within 0 – {total_dur:.1f}s.
- Do NOT overlap clips.
- Return between {CLIPS_MIN_COUNT} and {CLIPS_MAX_COUNT} clips. Only include a clip if it genuinely clears the bar above — do not pad to hit the minimum if the content isn't there.
- Be selective: a shorter list of strong clips is better than a longer list of weak ones.
- The title must be literally true and directly grounded in what is actually said or shown within this specific clip's transcript. Do not invent metaphors, comparisons, analogies, or claims that were not actually made. Punchy, bold, ALL-CAPS framing is encouraged — misrepresenting what happens in the clip is not, even if it would get more clicks. This applies to every platform the clip is posted to (YouTube, Instagram, TikTok) and matters for policy compliance, not just tone.
  Example — a clip where someone describes a stressful 14-hour flight delay:
    Good: "STRANDED AT THE AIRPORT FOR 14 HOURS"     (true — this is what's actually said)
    Bad:  "THE FLIGHT DELAY NASA TRIED TO COVER UP"  (invents a claim never made in the clip)

Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON.
Format:
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<punchy hook title, 5 words max, ALL CAPS, no emojis, must accurately reflect the clip's actual content — see rules above>",
    "reason": "<one sentence: what makes this moment viral>"
  }},
  ...
]"""

    print("Asking Claude to find viral moments…")
    with client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    # Extract the text block from the response
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        print("⚠ Claude returned an empty response.")
        return []

    # Parse JSON — strip accidental markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        clips = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠ Could not parse Claude's response as JSON: {e}")
        print("Raw response:", raw[:500])
        return []

    # Validate and sanitise each clip
    valid = []
    for item in clips:
        s = float(item.get("start", 0))
        e = float(item.get("end", 0))
        reason = item.get("reason", "")
        title = item.get("title", "")
        dur = e - s
        if dur < CLIPS_MIN_DURATION or dur > CLIPS_MAX_DURATION:
            print(f"  Skipping clip {s:.1f}–{e:.1f}s (duration {dur:.1f}s out of range)")
            continue
        if s < 0 or e > total_dur + 5:  # +5s tolerance
            print(f"  Skipping clip {s:.1f}–{e:.1f}s (out of video range)")
            continue
        valid.append({"start": round(s, 2), "end": round(e, 2), "title": title, "reason": reason})

    # Hard cap at CLIPS_MAX_COUNT
    if len(valid) > CLIPS_MAX_COUNT:
        print(f"  Capping at {CLIPS_MAX_COUNT} clips (Claude returned {len(valid)})")
        valid = valid[:CLIPS_MAX_COUNT]

    return valid


def find_tiktok_clips(transcript_path: str) -> list[dict]:
    """
    Send the transcript to Claude and get back ~62s TikTok-specific clip candidates.

    Unlike find_viral_clips (which hunts for short, single-hit viral moments),
    this pass looks for longer segments that open with a strong hook in the
    first 5-15s and have enough substance to sustain interest for a full
    minute — long enough to clear TikTok's 60s Creator Rewards Program
    threshold.

    Returns a list of dicts:
        [{"start": float, "end": float, "title": str, "reason": str, "hook_type": str}, ...]
    """
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    transcript_text = _build_transcript_text(transcript)
    total_dur = _total_duration(transcript)

    if not transcript_text.strip():
        print("⚠ Transcript appears empty — no TikTok clips to find.")
        return []

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    system_prompt = (
        "You are a senior TikTok editor who has grown multiple accounts to millions of "
        "followers, and you understand exactly how the algorithm and audience retention "
        "work for full-minute content, not just 15-second hits. You know that a video only "
        "earns Creator Rewards payouts if it runs 60+ seconds AND actually holds the "
        "viewer's attention that whole time — a great opening line followed by dead air "
        "gets abandoned and tanks completion rate. You are ruthlessly selective about "
        "which segments have real staying power."
    )

    user_prompt = f"""Below is a word-level transcript from a video. Total duration: {total_dur:.1f}s.

---
{transcript_text}
---

Your task: identify moments in this video that can sustain a ~{TIKTOK_TARGET_DURATION}s TikTok clip start-to-finish.

Requirements (a clip must meet ALL of these):
- Clip length must be {TIKTOK_MIN_DURATION}–{TIKTOK_MAX_DURATION} seconds (target ~{TIKTOK_TARGET_DURATION}s). This is a hard requirement — TikTok's Creator Rewards Program only pays out on videos 60+ seconds long.
- The first 5–{TIKTOK_HOOK_MAX_START} seconds of the clip must contain a strong hook: a direct question posed to the viewer, a bold/provocative claim, or a surprising/counterintuitive fact — something that makes a scroller stop and commit to watching instead of swiping past.
- Everything after the hook must have enough real substance — a developing story, an unfolding argument, escalating examples building toward a bigger payoff, or a payoff being built toward — to actually hold attention for the full ~{TIKTOK_TARGET_DURATION}s. An escalating list counts as sustaining substance if it's building toward a concrete, specific payoff (e.g., naming increasingly severe conditions before landing on the most surprising cured case) — reject a segment only if it's a strong opening line followed by genuine filler, verbatim repetition, or a topic change with nothing riding on it.
- A compelling moment doesn't have to use a story's full natural boundaries — if a longer story contains a segment that satisfies the hook-timing and duration requirements above, trim to that window rather than discarding the story because its untrimmed length or hook placement doesn't qualify.

Rules:
- Timestamps must be within 0 – {total_dur:.1f}s.
- Do NOT overlap clips.
- Only include a clip if it genuinely clears every requirement above — return however many segments qualify (including zero, if nothing in this video can sustain a full minute). Do not pad the list to hit any particular count.
- The title must be literally true and directly grounded in what is actually said or shown within this specific clip's transcript. Do not invent metaphors, comparisons, analogies, or claims that were not actually made. Punchy, bold, ALL-CAPS framing is encouraged — misrepresenting what happens in the clip is not, even if it would get more clicks. This matters for policy compliance, not just tone.

Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON.
Format:
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<punchy hook title, 5 words max, ALL CAPS, no emojis, must accurately reflect the clip's actual content — see rules above>",
    "reason": "<one sentence: what the hook is and why the rest of the clip sustains interest>",
    "hook_type": "<one of: question, bold_claim, surprising_fact>"
  }},
  ...
]"""

    print("Asking Claude to find ~62s TikTok moments…")
    with client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        print("⚠ Claude returned an empty response.")
        return []

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        clips = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠ Could not parse Claude's response as JSON: {e}")
        print("Raw response:", raw[:500])
        return []

    valid = []
    for item in clips:
        s = float(item.get("start", 0))
        e = float(item.get("end", 0))
        reason = item.get("reason", "")
        title = item.get("title", "")
        hook_type = item.get("hook_type", "")
        dur = e - s
        if dur < TIKTOK_MIN_DURATION or dur > TIKTOK_MAX_DURATION:
            print(f"  Skipping TikTok clip {s:.1f}–{e:.1f}s (duration {dur:.1f}s out of range)")
            continue
        if s < 0 or e > total_dur + 5:  # +5s tolerance
            print(f"  Skipping TikTok clip {s:.1f}–{e:.1f}s (out of video range)")
            continue
        valid.append({
            "start": round(s, 2),
            "end": round(e, 2),
            "title": title,
            "reason": reason,
            "hook_type": hook_type,
            "transcript_excerpt": _excerpt_for_range(transcript, s, e),
        })

    if len(valid) > TIKTOK_MAX_COUNT:
        print(f"  Capping at {TIKTOK_MAX_COUNT} TikTok clips (Claude returned {len(valid)})")
        valid = valid[:TIKTOK_MAX_COUNT]

    return valid


def save_clips_json(clips: list[dict], transcript_path: str, suffix: str = ".clips.json") -> str:
    """Save the clip list as a JSON file next to the transcript."""
    out_path = transcript_path.replace(".transcript.json", suffix)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)
    return out_path


def save_tiktok_clips_json(clips: list[dict], transcript_path: str) -> str:
    """Save the TikTok-length clip list as a JSON file next to the transcript."""
    return save_clips_json(clips, transcript_path, suffix=".tiktok_clips.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyzer.py <transcript.json>")
        print("  Requires ANTHROPIC_API_KEY environment variable.")
        sys.exit(1)

    transcript_file = sys.argv[1]

    if not os.path.exists(transcript_file):
        print(f"Error: file not found: {transcript_file}")
        sys.exit(1)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set.")
        print("  Run:  export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    clips = find_viral_clips(transcript_file)

    if not clips:
        print("No viral clips found.")
        sys.exit(0)

    out = save_clips_json(clips, transcript_file)

    print(f"\n✓ Found {len(clips)} viral clip(s) → {out}\n")
    for i, c in enumerate(clips, 1):
        print(f"  Clip {i}: {c['start']}s – {c['end']}s ({c['end']-c['start']:.1f}s)")
        print(f"    {c['reason']}")
