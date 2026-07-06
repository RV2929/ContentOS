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
THUMBNAILS_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "public", "thumbnails")
_WHOOSH_PATH = os.path.join(SOUNDS_DIR, "whoosh.wav")
_IMPACT_PATH = os.path.join(SOUNDS_DIR, "impact.wav")

# Output resolution for vertical clips (TikTok / Reels / Shorts)
OUT_WIDTH = 1080
OUT_HEIGHT = 1920

_FACE_ZOOM       = 3.5   # crop height = median_face_h × this → face + neck + shoulders
_FACE_Y_BIAS     = 0.35  # face centre sits this fraction from crop top (shoulders below)
_SMOOTH_ALPHA_X  = 0.25  # EMA α for horizontal pan (higher = more responsive)
_SMOOTH_ALPHA_Y  = 0.10  # EMA α for vertical tilt  (lower  = more stable)
_DETECT_WIDTH    = 640   # downscale width for face detection speed
_SAMPLE_INTERVAL = 0.5   # seconds between sampled frames


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


def _fill_none(times: list, values: list, default: float) -> list:
    """Linear interpolation + nearest-neighbour fill for None entries."""
    filled = list(values)
    n = len(filled)
    valids = [i for i, v in enumerate(filled) if v is not None]
    if not valids:
        return [default] * n
    # back-fill before first detection
    for i in range(valids[0]):
        filled[i] = filled[valids[0]]
    # forward-fill after last detection
    for i in range(valids[-1] + 1, n):
        filled[i] = filled[valids[-1]]
    # interpolate interior gaps
    i = valids[0]
    while i < valids[-1]:
        if filled[i] is None:
            j = i + 1
            while filled[j] is None:
                j += 1
            v0, v1 = filled[i - 1], filled[j]
            t0, t1 = times[i - 1], times[j]
            span = (t1 - t0) or 1.0
            for k in range(i, j):
                filled[k] = v0 + (v1 - v0) * (times[k] - t0) / span
        i += 1
    return filled


def _ema(values: list, alpha: float) -> list:
    """Exponential moving average."""
    if not values:
        return values
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def _build_crop_expr(keyframes: list, lo: int, hi: int) -> str:
    """
    FFmpeg expression for piecewise-linear interpolation between (t, px) keyframes.
    Uses sum-of-ramps: v0 + Σ delta_i * max(0, min(1, (t-t_{i-1}) / dt_i))
    Evaluates against the frame's original PTS so pass absolute timestamps.
    """
    if not keyframes:
        return str(lo)
    # Drop keyframes where value changed less than 1 px (shorten expression)
    simplified = [keyframes[0]]
    for kf in keyframes[1:]:
        if abs(kf[1] - simplified[-1][1]) >= 1.0:
            simplified.append(kf)
    if len(simplified) == 1:
        return f"max({lo},min({hi},{int(round(simplified[0][1]))}))"
    parts = [str(int(round(simplified[0][1])))]
    for i in range(1, len(simplified)):
        t0, v0 = simplified[i - 1]
        t1, v1 = simplified[i]
        dt    = t1 - t0
        delta = int(round(v1)) - int(round(v0))
        if dt <= 0 or delta == 0:
            continue
        parts.append(f"({delta})*max(0,min(1,(t-{t0:.3f})/{dt:.3f}))")
    inner = "+".join(parts)
    return f"max({lo},min({hi},{inner}))"


def _detect_face_track(video_path: str, start: float, end: float):
    """
    Read frames sequentially from [start, end], detect faces at every
    _SAMPLE_INTERVAL seconds, smooth the positions, and return a crop spec:

        (crop_w, crop_h, x_kf, y_kf)

    where x_kf / y_kf are [(t_rel_seconds, crop_left_px), ...].
    Returns None when OpenCV is unavailable or fewer than 3 frames detect a face
    (caller falls back to centre crop).
    """
    if not _OPENCV:
        return None

    cascade = _cv2.CascadeClassifier(
        _cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )

    cap    = _cv2.VideoCapture(video_path)
    fps    = cap.get(_cv2.CAP_PROP_FPS) or 25.0
    vid_w  = int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
    vid_h  = int(cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))

    det_scale = _DETECT_WIDTH / vid_w
    det_h     = int(vid_h * det_scale)

    # Seek to clip start, then read sequentially (much faster than random seeks).
    cap.set(_cv2.CAP_PROP_POS_FRAMES, int(start * fps))

    detections  = []   # (t_rel, cx_px | None, cy_px | None, fh_px | None)
    frame_idx   = 0
    next_sample = 0.0

    while True:
        t_rel = frame_idx / fps
        if (start + t_rel) >= end:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if t_rel >= next_sample:
            small = _cv2.resize(frame, (_DETECT_WIDTH, det_h))
            gray  = _cv2.cvtColor(small, _cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
            if len(faces):
                fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                detections.append((
                    t_rel,
                    (fx + fw / 2) / det_scale,   # cx in full-res px
                    (fy + fh / 2) / det_scale,   # cy in full-res px
                    fh / det_scale,              # face height in full-res px
                ))
            else:
                detections.append((t_rel, None, None, None))
            next_sample += _SAMPLE_INTERVAL
        frame_idx += 1
    cap.release()

    if not detections:
        return None

    valid = [(cx, cy, fh) for (_, cx, cy, fh) in detections if cx is not None]
    if len(valid) < 3:
        return None   # too few detections — caller uses centre crop

    # Fixed crop dimensions from median face height (multiples of 16 → exact 9:16 ratio)
    med_fh  = sorted(v[2] for v in valid)[len(valid) // 2]
    raw_ch  = med_fh * _FACE_ZOOM
    crop_h  = int(max(vid_h * 0.30, min(vid_h, raw_ch)) / 16) * 16
    crop_w  = crop_h * 9 // 16   # exact 9:16 for multiples of 16
    if crop_w > vid_w:            # very wide source: shrink to fit
        crop_w = (vid_w // 2) * 2
        crop_h = int(crop_w * 16 / 9 / 16) * 16

    # Fill detection gaps and smooth
    times   = [d[0] for d in detections]
    raw_cxs = [d[1] for d in detections]
    raw_cys = [d[2] for d in detections]

    sm_cx = _ema(_fill_none(times, raw_cxs, vid_w / 2), _SMOOTH_ALPHA_X)
    sm_cy = _ema(_fill_none(times, raw_cys, vid_h / 2), _SMOOTH_ALPHA_Y)

    # Convert face centres → crop top-left coordinates
    def to_x(cx):  return max(0, min(vid_w - crop_w, int(cx - crop_w / 2)))
    def to_y(cy):  return max(0, min(vid_h - crop_h, int(cy - crop_h * _FACE_Y_BIAS)))

    x_kf = [(t, to_x(cx)) for t, cx in zip(times, sm_cx)]
    y_kf = [(t, to_y(cy)) for t, cy in zip(times, sm_cy)]

    return crop_w, crop_h, x_kf, y_kf


def cut_and_crop(
    video_path: str,
    start: float,
    end: float,
    output_path: str,
    ass_path: str | None = None,
    face_track=None,
) -> str:
    """
    Cut [start, end] from video_path, crop to 9:16 with dynamic face tracking,
    overlay on a blurred background, burn captions, and save to output_path.

    face_track: return value of _detect_face_track — (crop_w, crop_h, x_kf, y_kf).
    None falls back to a static centre crop.

    Single FFmpeg pass:
      -ss before -i for fast input seek (frames keep original PTS in filter graph).
      filter_complex: split → blurred bg + face-tracked fg → overlay centred.
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

    # Foreground: face-tracked dynamic crop or centre fallback.
    if face_track is not None:
        crop_w, crop_h, x_kf, y_kf = face_track
        _cap = _cv2.VideoCapture(video_path)
        _vw  = int(_cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
        _vh  = int(_cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
        _cap.release()
        # -ss before -i keeps original PTS in filter graph → shift keyframes by start.
        x_expr = _build_crop_expr([(t + start, px) for t, px in x_kf], 0, _vw - crop_w)
        y_expr = _build_crop_expr([(t + start, px) for t, px in y_kf], 0, _vh - crop_h)
        fg_filter = (
            f"crop={crop_w}:{crop_h}:{x_expr}:{y_expr},"
            f"scale={OUT_WIDTH}:{OUT_HEIGHT}:flags=lanczos"
        )
    elif _OPENCV:
        # No face detected: tight 9:16 centre crop.
        _cap = _cv2.VideoCapture(video_path)
        _vw  = int(_cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
        _vh  = int(_cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
        _cap.release()
        _tw  = int(_vh * 9 / 16 / 16) * 16
        _tw  = min(_tw, _vw)
        _cx  = (_vw - _tw) // 2
        fg_filter = f"crop={_tw}:{_vh}:{_cx}:0,scale={OUT_WIDTH}:{OUT_HEIGHT}:flags=lanczos"
    else:
        # Pure-FFmpeg fallback (OpenCV not installed).
        _ew = "trunc(min(iw,ih*9/16)/2)*2"
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

    if ass_path:
        esc = ass_path.replace("\\", "\\\\").replace(":", "\\:")
        video_fc += f";[ov]subtitles='{esc}':fontsdir='/Users/rajvi/Library/Fonts'[vout]"
        v_map = "[vout]"
    else:
        v_map = "[ov]"

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
) -> list[str]:
    """
    Read clips JSON and produce cropped MP4s in CLIPS_DIR.

    If transcript_path is given, burns CapCut-style word-by-word captions
    and a bold hook title onto every clip using WhisperX word timestamps.

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

    os.makedirs(CLIPS_DIR, exist_ok=True)

    video_stem = os.path.splitext(os.path.basename(video_path))[0]
    output_paths = []

    for i, clip in enumerate(clips, 1):
        start = float(clip["start"])
        end = float(clip["end"])
        title = clip.get("title", "")
        reason = clip.get("reason", "")
        reason_slug = _safe_name(reason) if reason else f"clip{i}"

        filename = f"{_safe_name(video_stem)}_clip{i:02d}_{reason_slug}.mp4"
        out_path = os.path.join(CLIPS_DIR, filename)

        dur        = end - start
        face_track = _detect_face_track(video_path, start, end)
        if face_track:
            face_note = f"face tracked {face_track[0]}×{face_track[1]}px"
        else:
            face_note = "centre crop"
        print(f"  [{i}/{len(clips)}] {start:.1f}s – {end:.1f}s ({dur:.1f}s) [{face_note}] → {filename}")

        # Write a per-clip ASS subtitle file whenever we have captions or a title.
        ass_path = None
        if transcript_words or title:
            ass_content = _build_ass(transcript_words, title, start, end)
            fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix=f"contentos_clip{i:02d}_")
            with os.fdopen(fd, "w", encoding="utf-8") as af:
                af.write(ass_content)

        try:
            cut_and_crop(video_path, start, end, out_path, ass_path=ass_path, face_track=face_track)
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
        print("Usage: python clipper.py <video.mp4> <clips.json>")
        print("  Outputs vertical MP4s to the clips/ folder.")
        sys.exit(1)

    video = sys.argv[1]
    clips_json = sys.argv[2]

    if not os.path.exists(video):
        print(f"Error: video not found: {video}")
        sys.exit(1)
    if not os.path.exists(clips_json):
        print(f"Error: clips JSON not found: {clips_json}")
        sys.exit(1)

    print(f"Processing clips from: {os.path.basename(video)}")
    paths = process_clips(video, clips_json)

    if paths:
        print(f"\n✓ {len(paths)} clip(s) saved to: {CLIPS_DIR}")
        for p in paths:
            print(f"  {os.path.basename(p)}")
    else:
        print("No clips were produced.")
