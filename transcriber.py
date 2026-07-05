"""
Step 2 — Transcribe a video file using WhisperX.
Produces a JSON file with word-level timestamps next to the video file.
"""

import sys
import os
import json
import whisperx


# Add bundled ffmpeg to PATH so whisperx.load_audio() can find it
_FFMPEG_DIR = os.path.join(os.path.dirname(__file__), "venv", "bin")
os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")


def transcribe(video_path: str, model_size: str = "base") -> str:
    """
    Transcribe the audio from video_path.
    Returns the path to the saved JSON transcript.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    # ctranslate2 (WhisperX backend) only supports CUDA or CPU — not Apple MPS
    device = "cpu"
    print(f"Using device: {device}  |  model: {model_size}")

    compute_type = "float32"

    print("Loading Whisper model...")
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)

    print("Transcribing audio...")
    audio = whisperx.load_audio(video_path)
    result = model.transcribe(audio, batch_size=4)

    print("Aligning word timestamps...")
    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device
    )
    result = whisperx.align(
        result["segments"], align_model, metadata, audio, device,
        return_char_alignments=False,
    )

    # Save transcript as JSON alongside the video
    transcript_path = os.path.splitext(video_path)[0] + ".transcript.json"
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    word_count = sum(len(seg.get("words", [])) for seg in result["segments"])
    print(f"\n✓ Transcribed {word_count} words → {transcript_path}")
    return transcript_path


def load_transcript(transcript_path: str) -> dict:
    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python transcriber.py <video.mp4> [model_size]")
        print("  model sizes: tiny, base, small, medium, large-v2  (default: base)")
        sys.exit(1)

    video = sys.argv[1]
    size = sys.argv[2] if len(sys.argv) > 2 else "base"
    path = transcribe(video, model_size=size)
    print(f"Transcript saved to: {path}")
