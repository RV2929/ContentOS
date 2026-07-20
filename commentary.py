"""
Step 3.5 — Generate original spoken commentary for TikTok clips.

For each ~62s TikTok clip, asks Claude for a short (5-10s) commentary line that
references a specific, verifiable detail from that clip's own transcript
excerpt (not a generic reaction), then synthesizes it to speech with macOS's
built-in `say` command. The result is prepended as an intro segment by
clipper.py, giving TikTok's Creator Rewards "adds new ideas" review something
concrete and original to point to, beyond the reused source footage.
"""

import os
import re
import json
import subprocess
import tempfile
import anthropic

VOICE = "Samantha"
MAX_RETRIES = 2


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _quote_is_grounded(quote: str, excerpt: str) -> bool:
    """True if `quote` appears verbatim (case/whitespace-insensitive) inside excerpt.

    Guards against Claude writing a generic reaction and calling it "grounded" —
    the quoted phrase must actually be traceable to what was said in the clip.
    """
    quote_n = _normalize(quote)
    if len(quote_n) < 6:  # too short to be a real, checkable quote
        return False
    return quote_n in _normalize(excerpt)


def _generate_one(title: str, excerpt: str) -> str | None:
    """Ask Claude for a commentary line grounded in the clip's transcript excerpt.

    Retries (with feedback) if the returned quoted_reference isn't actually
    found in the excerpt — i.e. the commentary read like a generic template
    rather than genuine, specific insight. Returns None if it never grounds.
    """
    client = anthropic.Anthropic()

    feedback = ""
    for attempt in range(MAX_RETRIES + 1):
        prompt = f"""You are adding a short original commentary intro to a clip from a podcast/interview. It will be spoken aloud, before the clip itself plays.

Clip hook: "{title}"

Transcript excerpt for this exact clip:
---
{excerpt}
---
{feedback}
Write ONE spoken sentence (15-25 words, 5-10 seconds read aloud) of genuine editorial commentary — your own reaction, framing, or insight about this clip. It MUST reference a specific, concrete detail, quote, or fact that is actually said in the transcript excerpt above — not a vague, generic reaction that could apply to any clip (e.g. NOT "this part really surprised me because it's so relatable"). Ground it in something a viewer couldn't know just from the hook/title alone.

Reply ONLY with valid JSON: {{"commentary": "...", "quoted_reference": "<the exact phrase from the excerpt above that your commentary is grounded in>"}}"""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = re.sub(r"```(?:json)?|```", "", response.content[0].text).strip()
            data = json.loads(text)
            commentary = (data.get("commentary") or "").strip()
            quoted_reference = (data.get("quoted_reference") or "").strip()
        except Exception as exc:
            print(f"    ⚠ Commentary generation failed ({exc})")
            feedback = ""
            continue

        if commentary and _quote_is_grounded(quoted_reference, excerpt):
            return commentary

        print(f"    ⚠ Commentary attempt {attempt + 1} wasn't grounded in the transcript — retrying")
        feedback = (
            f'\nYour previous attempt was: "{commentary}" (claimed reference: "{quoted_reference}"). '
            f'That reference was not found verbatim in the excerpt above. Try again, and quote '
            f'something that actually appears in the excerpt, word for word.\n'
        )

    return None


def add_commentary(clips: list[dict]) -> list[dict]:
    """
    Populate clip["commentary_text"] and clip["commentary_audio_path"] for each
    TikTok clip that has a transcript_excerpt, using a Claude commentary line
    grounded in that excerpt and synthesized to speech via macOS `say`.

    Clips where a grounded line can't be produced after retries, or where TTS
    fails, are left without these fields — clipper.py skips the intro for those
    rather than failing the clip.
    """
    for i, clip in enumerate(clips, 1):
        excerpt = clip.get("transcript_excerpt", "")
        if not excerpt.strip():
            continue

        print(f"  [{i}/{len(clips)}] Generating commentary…")
        commentary = _generate_one(clip.get("title", ""), excerpt)
        if not commentary:
            print(f"    ✗ Could not produce a grounded commentary line — skipping intro for this clip")
            continue

        fd, audio_path = tempfile.mkstemp(suffix=".aiff", prefix=f"contentos_commentary{i:02d}_")
        os.close(fd)
        r = subprocess.run(["say", "-v", VOICE, "-o", audio_path, commentary], capture_output=True)
        if r.returncode != 0:
            print(f"    ✗ TTS synthesis failed — skipping intro for this clip")
            if os.path.exists(audio_path):
                os.unlink(audio_path)
            continue

        clip["commentary_text"] = commentary
        clip["commentary_audio_path"] = audio_path
        print(f"    ✓ \"{commentary}\"")

    return clips
