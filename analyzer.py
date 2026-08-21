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
        model="claude-sonnet-5",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    print(f"  stop_reason={response.stop_reason} output_tokens={response.usage.output_tokens}")

    # Extract the text block from the response
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        if response.stop_reason == "max_tokens":
            print("⚠ Claude hit max_tokens before emitting any output (thinking consumed the full budget) — increase max_tokens.")
        else:
            print(f"⚠ Claude returned an empty response (stop_reason={response.stop_reason}).")
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



LONGFORM_MIN_DURATION = 480   # seconds — 8 minutes
LONGFORM_MAX_DURATION = 900   # seconds — 15 minutes
LONGFORM_MAX_COUNT = 3        # soft cap — Claude returns fewer (even zero) if the content isn't there


def find_long_segments(transcript_path: str) -> list[dict]:
    """
    Send the transcript to Claude and get back up to LONGFORM_MAX_COUNT
    long-form segments (8-15 min each), each covering one complete story,
    argument, or topic start-to-finish — suitable as standalone long-form
    YouTube uploads.

    Unlike find_viral_clips, this hunts for cohesive,
    complete arcs rather than punchy hooks. Returns an empty list if
    nothing in the video sustains even one full cohesive arc that long —
    this is meant to be rare and high-value, not high-volume, so there is
    no minimum count to pad toward. Returned segments never overlap each
    other.

    Returns a list of dicts:
        [{"start": float, "end": float, "title": str, "reason": str}, ...]
    """
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    transcript_text = _build_transcript_text(transcript)
    total_dur = _total_duration(transcript)

    if not transcript_text.strip():
        print("⚠ Transcript appears empty — no long-form segments to find.")
        return []

    if total_dur < LONGFORM_MIN_DURATION:
        print(f"  Source video is only {total_dur:.0f}s — too short for an 8-15 min long-form segment.")
        return []

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    system_prompt = (
        "You are a senior long-form YouTube editor who specializes in finding the best "
        "standalone segments inside a longer recording — not punchy hooks or viral "
        "moments, but complete, self-contained pieces of content: a full story with a "
        "beginning, middle, and end; a complete argument that's actually developed and "
        "resolved; or a topic that's explored in real depth rather than just mentioned. "
        "You know the difference between content that merely started strong and content "
        "that holds together as a coherent, standalone watch for 8-15 minutes straight. "
        "You are extremely selective — most videos do not contain even one segment that "
        "clears this bar, and you would rather return fewer (even zero) than force a "
        "mediocre one just to fill out a list."
    )

    user_prompt = f"""Below is a word-level transcript from a video. Total duration: {total_dur:.1f}s.

---
{transcript_text}
---

Your task: find the best long-form segment(s) in this video, suitable for upload as standalone regular (non-Shorts) YouTube videos — up to {LONGFORM_MAX_COUNT} of them.

Requirements (each segment must meet ALL of these):
- Length must be {LONGFORM_MIN_DURATION}–{LONGFORM_MAX_DURATION} seconds ({LONGFORM_MIN_DURATION/60:.0f}–{LONGFORM_MAX_DURATION/60:.0f} minutes).
- Must be cohesive: a complete story, a fully developed argument, or a topic explored in depth — with a real beginning, middle, and end. A viewer who watches only this segment, with no other context, should feel they got a complete piece of content, not an arbitrary excerpt.
- Do not chase a hook or a viral opening line — this is not short-form. Prioritize substance and completeness over a punchy start.
- Timestamps must be within 0 – {total_dur:.1f}s.

Rules:
- Return up to {LONGFORM_MAX_COUNT} segments, and only as many as genuinely clear the bar above — return fewer, or an empty list, if this video doesn't contain that many complete cohesive arcs. Do not force a segment that merely starts strong or stitches together unrelated moments just to reach {LONGFORM_MAX_COUNT}.
- Segments must NOT overlap each other — each must cover a distinct stretch of the video.
- The title must be literally true and directly grounded in what is actually said or shown within this segment's transcript. Do not invent claims, comparisons, or framing not actually present.

Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON.
Format (a list, 0 to {LONGFORM_MAX_COUNT} items):
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<descriptive long-form YouTube title, plain framing, not a punchy hook>",
    "reason": "<one or two sentences: what story/argument/topic this segment covers, and why it's cohesive start-to-finish>"
  }},
  ...
]

Or, if nothing qualifies:
[]"""

    print("Asking Claude to find long-form segments…")
    with client.messages.stream(
        model="claude-sonnet-5",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    print(f"  stop_reason={response.stop_reason} output_tokens={response.usage.output_tokens}")

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        if response.stop_reason == "max_tokens":
            print("⚠ Claude hit max_tokens before emitting any output (thinking consumed the full budget) — increase max_tokens.")
        else:
            print(f"⚠ Claude returned an empty response (stop_reason={response.stop_reason}).")
        return []

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠ Could not parse Claude's response as JSON: {e}")
        print("Raw response:", raw[:500])
        return []

    if not items:
        print("  → no segment could sustain a complete long-form arc")
        return []

    valid: list[dict] = []
    for item in items:
        s = float(item.get("start", 0))
        e = float(item.get("end", 0))
        title = item.get("title", "")
        reason = item.get("reason", "")
        dur = e - s

        if dur < LONGFORM_MIN_DURATION or dur > LONGFORM_MAX_DURATION:
            print(f"  Skipping long-form segment {s:.1f}–{e:.1f}s (duration {dur:.1f}s out of range)")
            continue
        if s < 0 or e > total_dur + 5:  # +5s tolerance
            print(f"  Skipping long-form segment {s:.1f}–{e:.1f}s (out of video range)")
            continue
        if any(s < v["end"] and e > v["start"] for v in valid):
            print(f"  Skipping long-form segment {s:.1f}–{e:.1f}s (overlaps another accepted segment)")
            continue

        valid.append({
            "start": round(s, 2),
            "end": round(e, 2),
            "title": title,
            "reason": reason,
            "transcript_excerpt": _excerpt_for_range(transcript, s, e),
        })

    if len(valid) > LONGFORM_MAX_COUNT:
        print(f"  Capping at {LONGFORM_MAX_COUNT} long-form segments (Claude returned {len(valid)})")
        valid = valid[:LONGFORM_MAX_COUNT]

    return valid


def save_clips_json(clips: list[dict], transcript_path: str, suffix: str = ".clips.json") -> str:
    """Save the clip list as a JSON file next to the transcript."""
    out_path = transcript_path.replace(".transcript.json", suffix)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)
    return out_path



def save_long_segment_json(segments: list[dict], transcript_path: str) -> str:
    """Save the long-form segment list next to the transcript."""
    return save_clips_json(segments, transcript_path, suffix=".longform.json")


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
