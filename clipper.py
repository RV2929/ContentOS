"""
Step 4 & 5 — Cut viral clips and crop to 9:16 vertical format.
Reads a .clips.json file, cuts each segment from the source video,
center-crops to 9:16, burns CapCut-style word-by-word captions, and
exports MP4s to the clips/ folder.
"""

import sys
import os
import json
import subprocess
import tempfile
import imageio_ffmpeg

try:
    import cv2 as _cv2
    _OPENCV = True
except ImportError:
    _OPENCV = False

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
CLIPS_DIR = os.path.join(os.path.dirname(__file__), "clips")
SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "sounds")


def clips_dir_for(channel: str = "podcast") -> str:
    """clips/<channel>/ — keeps podcast, football, and streamers output physically separate."""
    channel = channel if channel in ("podcast", "football", "streamers") else "podcast"
    return os.path.join(CLIPS_DIR, channel)

THUMBNAILS_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "public", "thumbnails")
_WHOOSH_PATH = os.path.join(SOUNDS_DIR, "whoosh.wav")
_IMPACT_PATH = os.path.join(SOUNDS_DIR, "impact.wav")

# Output resolution for vertical clips (TikTok / Reels / Shorts)
OUT_WIDTH = 1080
OUT_HEIGHT = 1920

# How far to zoom out from the tight 9:16 face crop.
# 1.0 = tight (face fills screen). 2.0 = show ~2× more horizontal context
# (chest/shoulders visible). Increase toward 3.0 for more breathing room.
FACE_CROP_EXPAND = 2.0

# Brand watermark burned into every clip (bottom-left corner, below the caption band).
WATERMARK_TEXT = "ContentOS29"
WATERMARK_FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _fmt_ass_time(seconds: float) -> str:
    """Format seconds as an ASS timestamp (H:MM:SS.CC)."""
    cs = int(round(seconds * 100))
    h = cs // 360000;  cs %= 360000
    m = cs // 6000;    cs %= 6000
    s = cs // 100;     cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _extract_words(transcript: dict) -> list[dict]:
    """Flatten all word-level entries from a WhisperX transcript."""
    words = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            if "start" in w and "end" in w and w.get("word", "").strip():
                words.append(w)
    return words


# ASS style definitions — CapCut look: bold white text, black outline
# PlayRes matches the output frame so font sizes are in screen pixels.
_ASS_HEADER = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_WIDTH}
PlayResY: {OUT_HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,5,2,2,40,40,480,1
Style: Title,Arial,72,&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,8,40,40,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_escape(text: str) -> str:
    """Strip ASS control characters from plain text."""
    return text.replace("\\", "").replace("{", "").replace("}", "")


# How many words appear on screen at once in the caption band.
_WORDS_PER_LINE = 4

def _split_emoji_suffix(text: str) -> tuple[str, str]:
    """Split 'HOOK TEXT 🔥😱' into ('HOOK TEXT', '🔥😱').

    Title words are ALL CAPS ASCII; emojis are non-ASCII (ord > 127).
    Walk backwards past any non-ASCII chars and spaces to find the split point.
    """
    s = text.rstrip()
    i = len(s)
    while i > 0 and (ord(s[i - 1]) > 127 or s[i - 1] == " "):
        i -= 1
    return s[:i].rstrip(), s[i:].strip()


def _build_ass(words: list[dict], title: str, clip_start: float, clip_end: float) -> str:
    """
    Return ASS subtitle content with karaoke-style yellow word highlighting.

    Words are grouped into lines of _WORDS_PER_LINE.  For each timing window
    (one per word), all words in the group are shown: the currently-spoken word
    is coloured yellow via an inline \\c override; the rest stay white (the
    style default).  The black outline is part of the style and is unaffected
    by the fill-colour override, so it stays black on both white and yellow
    words.
    """
    clip_dur = clip_end - clip_start

    # Filter words that fall inside this clip and normalise to clip-relative time.
    clip_words = []
    for w in words:
        ws = float(w.get("start", -1))
        we = float(w.get("end", -1))
        if ws < 0 or we < 0:
            continue
        if we <= clip_start or ws >= clip_end:
            continue
        rel_s = max(0.0, ws - clip_start)
        rel_e = min(clip_dur, we - clip_start)
        if rel_e <= rel_s:
            continue
        word = _ass_escape(w.get("word", "").strip().upper())
        if not word:
            continue
        clip_words.append({"word": word, "start": rel_s, "end": rel_e})

    events = [_ASS_HEADER.rstrip()]

    # Hook title — shown for the full clip duration at the top.
    if title:
        text_part, _ = _split_emoji_suffix(title)
        title_content = _ass_escape(text_part)
        events.append(
            f"Dialogue: 0,{_fmt_ass_time(0)},{_fmt_ass_time(clip_dur)},Title,,0,0,0,,{title_content}"
        )

    # Split into display groups and emit one Dialogue event per word transition.
    groups = [clip_words[i:i + _WORDS_PER_LINE] for i in range(0, len(clip_words), _WORDS_PER_LINE)]
    for group in groups:
        for j, active in enumerate(group):
            # Keep the active word lit until the next word starts (fills inter-word gaps).
            state_start = active["start"]
            state_end = group[j + 1]["start"] if j + 1 < len(group) else active["end"]
            if state_end <= state_start:
                state_end = state_start + 0.04

            # Build the line: yellow for the active word, white (default) for the rest.
            # ASS colour format is &HAABBGGRR — yellow = &H0000FFFF (A=0,B=0,G=FF,R=FF).
            parts = []
            for k, w in enumerate(group):
                if k == j:
                    parts.append(f"{{\\c&H0000FFFF&}}{w['word']}{{\\r}}")
                else:
                    parts.append(w["word"])

            events.append(
                f"Dialogue: 0,{_fmt_ass_time(state_start)},{_fmt_ass_time(state_end)},"
                f"Caption,,0,0,0,,{' '.join(parts)}"
            )

    return "\n".join(events) + "\n"


def _ensure_sounds() -> bool:
    """Generate whoosh and impact WAV files into SOUNDS_DIR if they don't exist."""
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    ok = True

    if not os.path.exists(_WHOOSH_PATH):
        # Sine sweep 2 kHz → 500 Hz with exponential amplitude decay
        r = subprocess.run([
            FFMPEG, "-y", "-f", "lavfi",
            "-i", "aevalsrc=0.25*sin(6.28*(2000*t-1500*t*t))*exp(-t*3):s=44100:d=0.6",
            "-af", "afade=t=in:st=0:d=0.02,afade=t=out:st=0.5:d=0.1,aformat=channel_layouts=stereo",
            _WHOOSH_PATH,
        ], capture_output=True)
        if r.returncode != 0:
            print("  ⚠ Could not generate whoosh SFX")
            ok = False

    if not os.path.exists(_IMPACT_PATH):
        # Low-frequency thump (80 Hz + 180 Hz harmonic, very fast decay)
        r = subprocess.run([
            FFMPEG, "-y", "-f", "lavfi",
            "-i", "aevalsrc=0.5*sin(6.28*80*t)*exp(-t*20)+0.2*sin(6.28*180*t)*exp(-t*30):s=44100:d=0.3",
            "-af", "afade=t=in:st=0:d=0.005,aformat=channel_layouts=stereo",
            _IMPACT_PATH,
        ], capture_output=True)
        if r.returncode != 0:
            print("  ⚠ Could not generate impact SFX")
            ok = False

    return ok


def _safe_name(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a safe filename fragment."""
    keep = []
    for ch in text:
        if ch.isalnum() or ch in " -_":
            keep.append(ch)
    return "".join(keep).strip().replace(" ", "_")[:max_len]


def _detect_face_x(video_path: str, start: float, end: float) -> float:
    """
    Sample 8 frames from [start, end] and return the median face-centre X
    as a fraction of frame width (0.0–1.0).
    Falls back to 0.5 (centre crop) if OpenCV is unavailable or no face found.
    """
    if not _OPENCV:
        return 0.5

    cascade = _cv2.CascadeClassifier(
        _cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    cap = _cv2.VideoCapture(video_path)
    fps = cap.get(_cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(_cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH))

    start_f = int(start * fps)
    end_f = min(int(end * fps), total - 1)
    n = 8
    samples = [
        int(start_f + (end_f - start_f) * i / max(n - 1, 1))
        for i in range(n)
    ]

    face_xs = []
    for fnum in samples:
        cap.set(_cv2.CAP_PROP_POS_FRAMES, fnum)
        ret, frame = cap.read()
        if not ret:
            continue
        gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50)
        )
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            face_xs.append((x + w / 2) / frame_w)

    cap.release()

    if not face_xs:
        return 0.5

    face_xs.sort()
    mid = len(face_xs) // 2
    return face_xs[mid] if len(face_xs) % 2 else (face_xs[mid - 1] + face_xs[mid]) / 2


def cut_and_crop(
    video_path: str,
    start: float,
    end: float,
    output_path: str,
    ass_path: str | None = None,
    face_x: float = 0.5,
) -> str:
    """
    Cut [start, end] from video_path, center-crop to 9:16, save to output_path.

    If ass_path is given, burns in captions from that ASS subtitle file.

    Uses a single FFmpeg pass:
      1. Fast input seek (-ss before -i) lands on a keyframe near `start`
      2. -t limits duration
      3. crop= filter: full height, 9/16 * height wide, centered horizontally
      4. scale= to OUT_WIDTH x OUT_HEIGHT
      5. subtitles= burns ASS captions (title + word-by-word)
      6. libx264 + AAC re-encode for compatibility
    """
    duration = round(end - start, 3)
    if duration <= 0:
        raise ValueError(f"Invalid clip: start={start} >= end={end}")

    # Background: scale to fill 9:16 (cover), then blur.
    bg_filter = (
        f"scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={OUT_WIDTH}:{OUT_HEIGHT},"
        f"boxblur=20:5"
    )

    # Foreground: face-aware crop, scaled to fit (no padding — overlaid on blurred bg).
    if _OPENCV:
        _cap = _cv2.VideoCapture(video_path)
        _vid_w = int(_cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
        _vid_h = int(_cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
        _cap.release()

        _tight_w = (int(_vid_h * 9 / 16) // 2) * 2
        _crop_w = (min(int(_tight_w * FACE_CROP_EXPAND), _vid_w) // 2) * 2
        _crop_x = max(0, min(_vid_w - _crop_w, int(face_x * _vid_w - _crop_w / 2)))

        _sf = min(OUT_WIDTH / _crop_w, OUT_HEIGHT / _vid_h)
        _sw = (int(_crop_w * _sf) // 2) * 2
        _sh = (int(_vid_h * _sf) // 2) * 2

        fg_filter = f"crop={_crop_w}:ih:{_crop_x}:0,scale={_sw}:{_sh}:flags=lanczos"
    else:
        _er = 9 / 16 * FACE_CROP_EXPAND
        _ew = f"trunc(min(iw,ih*{_er})/2)*2"
        fg_filter = (
            f"crop={_ew}:ih:(iw-{_ew})/2:0,"
            f"scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos"
        )

    # Build filter_complex: split source → blur bg + crop fg → overlay centred.
    video_fc = (
        f"[0:v]split=2[bg_src][fg_src];"
        f"[bg_src]{bg_filter}[bg];"
        f"[fg_src]{fg_filter}[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[ov]"
    )

    # Brand watermark: small, semi-transparent text in the bottom-left corner,
    # tucked under the caption band (Caption style MarginV=480 keeps captions
    # well above the bottom edge, so this never overlaps them).
    wm_font = WATERMARK_FONT.replace("\\", "\\\\").replace(":", "\\:")
    video_fc += (
        f";[ov]drawtext=text='{WATERMARK_TEXT}':fontfile='{wm_font}':fontsize=32:"
        f"fontcolor=white@0.55:x=28:y=h-th-28:box=1:boxcolor=black@0.35:boxborderw=10[wm]"
    )

    if ass_path:
        esc = ass_path.replace("\\", "\\\\").replace(":", "\\:")
        video_fc += f";[wm]subtitles='{esc}':fontsdir='/Users/rajvi/Library/Fonts'[vout]"
        v_map = "[vout]"
    else:
        v_map = "[wm]"

    sfx = os.path.exists(_WHOOSH_PATH) and os.path.exists(_IMPACT_PATH)
    if sfx:
        # Mix whoosh (t=0) and impact (t=80ms) at subtle levels under the voice.
        # normalize=0: direct sum so the main audio stays at full volume.
        audio_fc = (
            "[1:a]volume=0.12[w];"
            "[2:a]volume=0.15,adelay=80|80[im];"
            "[0:a][w][im]amix=inputs=3:duration=first:normalize=0[aout]"
        )
        cmd = [
            FFMPEG, "-y",
            "-ss", str(start), "-i", video_path,
            "-i", _WHOOSH_PATH,
            "-i", _IMPACT_PATH,
            "-t", str(duration),
            "-filter_complex", video_fc + ";" + audio_fc,
            "-map", v_map,
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            FFMPEG, "-y",
            "-ss", str(start), "-i", video_path,
            "-t", str(duration),
            "-filter_complex", video_fc,
            "-map", v_map,
            "-map", "0:a",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed for clip {start}–{end}s:\n{result.stderr[-1000:]}"
        )
    return output_path


def _generate_thumbnail(clip_path: str) -> None:
    """Extract a frame at 1 s from the finished clip and save as JPG for the dashboard."""
    try:
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)
        stem = os.path.splitext(os.path.basename(clip_path))[0]
        thumb_path = os.path.join(THUMBNAILS_DIR, f"{stem}.jpg")
        cmd = [
            FFMPEG, "-y",
            "-ss", "00:00:01",
            "-i", clip_path,
            "-vframes", "1",
            "-q:v", "3",
            thumb_path,
        ]
        subprocess.run(cmd, capture_output=True, check=False)
    except Exception:
        pass  # thumbnails are optional — never break clip production


def process_clips(
    video_path: str,
    clips_json_path: str,
    transcript_path: str | None = None,
    channel: str = "podcast",
    clip_label: str = "clip",
) -> list[str]:
    """
    Read clips JSON and produce cropped MP4s in clips/<channel>/.

    If transcript_path is given, burns CapCut-style word-by-word captions
    and a bold hook title onto every clip using WhisperX word timestamps.

    clip_label distinguishes output filenames when multiple clip sets are cut
    from the same video (e.g. "clip" for short viral clips vs "tiktok" for the
    ~62s TikTok-length pass) so they don't collide or get mixed up.

    Returns list of output file paths.
    """
    _ensure_sounds()

    with open(clips_json_path, "r", encoding="utf-8") as f:
        clips = json.load(f)

    if not clips:
        print("No clips to process.")
        return []

    # Load word timestamps from the WhisperX transcript for caption burn-in.
    transcript_words: list[dict] = []
    if transcript_path and os.path.exists(transcript_path):
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_words = _extract_words(json.load(f))
        print(f"  Loaded {len(transcript_words)} word timestamps for captions.")

    out_dir = clips_dir_for(channel)
    os.makedirs(out_dir, exist_ok=True)

    video_stem = os.path.splitext(os.path.basename(video_path))[0]
    output_paths = []

    for i, clip in enumerate(clips, 1):
        start = float(clip["start"])
        end = float(clip["end"])
        title = clip.get("title", "")
        reason = clip.get("reason", "")
        reason_slug = _safe_name(reason) if reason else f"clip{i}"

        filename = f"{_safe_name(video_stem)}_{clip_label}{i:02d}_{reason_slug}.mp4"
        out_path = os.path.join(out_dir, filename)

        dur = end - start
        face_x = _detect_face_x(video_path, start, end)
        face_note = f"face @ {face_x:.0%}" if face_x != 0.5 else "centre crop"
        print(f"  [{i}/{len(clips)}] {start:.1f}s – {end:.1f}s ({dur:.1f}s) [{face_note}] → {filename}")

        # Write a per-clip ASS subtitle file whenever we have captions or a title.
        ass_path = None
        if transcript_words or title:
            ass_content = _build_ass(transcript_words, title, start, end)
            fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix=f"contentos_clip{i:02d}_")
            with os.fdopen(fd, "w", encoding="utf-8") as af:
                af.write(ass_content)

        try:
            cut_and_crop(video_path, start, end, out_path, ass_path=ass_path, face_x=face_x)
            _generate_thumbnail(out_path)
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"    ✓ Saved ({size_mb:.1f} MB)")
            output_paths.append(out_path)
        except (RuntimeError, ValueError) as e:
            print(f"    ✗ Failed: {e}")
        finally:
            if ass_path and os.path.exists(ass_path):
                os.unlink(ass_path)

    return output_paths


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python clipper.py <video.mp4> <clips.json> [--channel podcast|football|streamers]")
        print("  Outputs vertical MP4s to clips/<channel>/.")
        sys.exit(1)

    video = sys.argv[1]
    clips_json = sys.argv[2]
    cli_channel = "podcast"
    if "--channel" in sys.argv:
        idx = sys.argv.index("--channel")
        if idx + 1 < len(sys.argv):
            cli_channel = sys.argv[idx + 1]

    if not os.path.exists(video):
        print(f"Error: video not found: {video}")
        sys.exit(1)
    if not os.path.exists(clips_json):
        print(f"Error: clips JSON not found: {clips_json}")
        sys.exit(1)

    print(f"Processing clips from: {os.path.basename(video)}")
    paths = process_clips(video, clips_json, channel=cli_channel)

    if paths:
        print(f"\n✓ {len(paths)} clip(s) saved to: {clips_dir_for(cli_channel)}")
        for p in paths:
            print(f"  {os.path.basename(p)}")
    else:
        print("No clips were produced.")
