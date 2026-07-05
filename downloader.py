"""
Step 1 — Download a YouTube video using yt-dlp.
Downloads the best available quality up to 1080p and saves it to the 'downloads' folder.
"""

import sys
import os
import yt_dlp
import imageio_ffmpeg


DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def download_video(url: str) -> str:
    """Download a YouTube video and return the path to the saved file."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    ydl_opts = {
        # Best video up to 1080p merged with best audio, saved as mp4
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s"),
        "ffmpeg_location": os.path.join(os.path.dirname(__file__), "venv", "bin"),
        "quiet": False,
        "no_warnings": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # yt-dlp may change extension after merge; ensure .mp4
        if not filename.endswith(".mp4"):
            filename = os.path.splitext(filename)[0] + ".mp4"

    if not os.path.exists(filename):
        raise FileNotFoundError(f"Expected output file not found: {filename}")

    size_mb = os.path.getsize(filename) / (1024 * 1024)
    print(f"\n✓ Downloaded: {os.path.basename(filename)} ({size_mb:.1f} MB)")
    return filename


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python downloader.py <youtube_url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Downloading: {url}")
    path = download_video(url)
    print(f"Saved to: {path}")
