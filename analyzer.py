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


CLIPS_MIN_COUNT = 3
CLIPS_MAX_COUNT = 10


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

Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON.
Format:
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<punchy hook title, 5 words max, ALL CAPS, no emojis>",
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


def save_clips_json(clips: list[dict], transcript_path: str) -> str:
    """Save the clip list as a JSON file next to the transcript."""
    out_path = transcript_path.replace(".transcript.json", ".clips.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)
    return out_path


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
