#!/usr/bin/env python3
"""
MP3 to M4B Audiobook Converter

Converts a folder of MP3 files into a single M4B audiobook file compatible
with Apple Books. Files are sorted by leading index number (01.mp3, 02.mp3, ...).

Features:
  - Each MP3 becomes one chapter, titled from its TIT2 ID3 tag
  - Book title taken from TALB tag, falls back to folder name
  - Author taken from TPE1 tag, falls back to folder name if in "Author - Book" format
  - Cover artwork embedded from ID3 tags or image file in folder
  - Parallel encoding using all available CPU cores for fast conversion
  - Interactive confirmation of chapter list, title and author before conversion

Usage:
    python mp3_to_m4b.py <input_folder> [-w N]
    python mp3_to_m4b.py <input_folder> [output] [-w N]

    input_folder    Folder containing MP3 files
    output          Output file path (default: <input_folder>.m4b next to input folder)

Options:
    -w, --workers N     Number of parallel encoding workers (default: all CPU cores)

Examples:
    python mp3_to_m4b.py ~/Downloads/"My Book"
    python mp3_to_m4b.py ~/Downloads/"My Book" ~/Desktop/"My Book.m4b"
    python mp3_to_m4b.py ~/Downloads/"My Book" -w 6

Requirements:
    - ffmpeg (must be installed and in PATH, e.g. brew install ffmpeg)
    - Python 3.7+
    - pip install mutagen tqdm
"""

import sys
import os
import subprocess
import tempfile
import glob
import re
from pathlib import Path
import concurrent.futures
import threading

# --- Auto-install dependencies ---
for pkg in ("mutagen", "tqdm"):
    try:
        __import__(pkg)
    except ImportError:
        print(f"Installing {pkg}...")
        subprocess.run([sys.executable, "-m", "pip", "install", pkg,
                        "--break-system-packages", "-q"], check=True)

from tqdm import tqdm

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, APIC
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    print("Warning: mutagen unavailable — tags/artwork may not be read.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd):
    """Run a command silently, raise on error."""
    return subprocess.run(cmd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ffmpeg_with_progress(cmd, duration_sec, desc="", position=0):
    """
    Run an ffmpeg command with a live tqdm progress bar.
    Uses ffmpeg's -progress pipe:1 to stream progress events.
    duration_sec: expected output duration (for % calculation).
    """
    import threading
    full_cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats"] + [cmd[-1]]
    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    # Drain stderr in background to prevent pipe buffer deadlock
    stderr_lines = []
    def drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    bar = tqdm(
        total=100,
        desc=desc,
        unit="%",
        ncols=72,
        bar_format="  {desc:<28} |{bar}| {n:3d}%  [{elapsed}<{remaining}]",
        leave=False,
        position=position,
    )
    last_pct = 0
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=")[1])
                    if duration_sec > 0:
                        pct = min(int(us / (duration_sec * 1_000_000) * 100), 100)
                        if pct > last_pct:
                            bar.update(pct - last_pct)
                            last_pct = pct
                except ValueError:
                    pass
            elif line == "progress=end":
                if last_pct < 100:
                    bar.update(100 - last_pct)
        proc.wait()
        t.join()
    finally:
        bar.close()
    if proc.returncode != 0:
        print(f"\nffmpeg error:\n{''.join(stderr_lines)}", file=sys.stderr)
        raise subprocess.CalledProcessError(proc.returncode, full_cmd)


def get_duration(path):
    """Return duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def get_audio_info(path):
    """Return (bitrate_kbps, channels) of the first audio stream via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=bit_rate,channels",
         "-of", "default=noprint_wrappers=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    bitrate, channels = None, None
    for line in result.stdout.splitlines():
        if line.startswith("bit_rate="):
            try:
                bitrate = max(1, int(line.split("=")[1]) // 1000)  # bps → kbps
            except ValueError:
                pass
        elif line.startswith("channels="):
            try:
                channels = int(line.split("=")[1])
            except ValueError:
                pass
    return bitrate, channels


def sanitize(value):
    """Strip control characters that corrupt terminal output."""
    return str(value).replace("\r", "").replace("\n", " ").strip()


def read_mp3_tags(path):
    """Extract ID3 tags and embedded artwork from an MP3."""
    tags = {}
    artwork = None
    if not MUTAGEN_AVAILABLE:
        return tags, artwork
    try:
        audio = MP3(path, ID3=ID3)
        id3 = audio.tags
        if id3:
            for frame, key in [("TIT2","title"),("TALB","album"),("TPE1","artist"),
                                ("TPE2","album_artist"),("TDRC","year"),("TCON","genre")]:
                if frame in id3:
                    tags[key] = sanitize(id3[frame])
            for key in id3.keys():
                if key.startswith("APIC"):
                    apic = id3[key]
                    artwork = (apic.data, apic.mime)
                    break
    except Exception as e:
        print(f"  Warning reading tags from {Path(path).name}: {e}")
    return tags, artwork


def hms(seconds):
    """Full precision H:MM:SS.mmm — used for ffmetadata chapter timestamps."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def dur(seconds):
    """Human-readable H:MM:SS — used for display only."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}"



def get_mp3_title(path):
    """Return TIT2 tag from an MP3, or None if not set."""
    if not MUTAGEN_AVAILABLE:
        return None
    try:
        audio = MP3(path, ID3=ID3)
        if audio.tags and "TIT2" in audio.tags:
            return sanitize(audio.tags["TIT2"]) or None
    except Exception:
        pass
    return None

def index_sort_key(path):
    """Sort by leading integer in filename (01.mp3, 02.mp3, ...).
    Falls back to alphabetical for names without a leading number."""
    stem = Path(path).stem
    match = re.match(r"^(\d+)", stem)
    return (int(match.group(1)), stem) if match else (float("inf"), stem)


def restore_terminal():
    """Re-enable CR→NL translation that ffmpeg may have disabled as a subprocess."""
    try:
        import termios
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[0] |= termios.ICRNL   # map CR to NL on input
        attrs[3] |= termios.ICANON  # canonical (line-buffered) mode
        attrs[3] |= termios.ECHO    # echo input characters
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    except Exception:
        pass


def prompt(label, default):
    """Show prompt with current value in brackets. Enter keeps it, typing replaces it.
    Reads stdin directly to avoid Warp terminal input() quirks."""
    restore_terminal()
    sys.stdout.write(f"  {label:<8} [{default}]: ")
    sys.stdout.flush()
    value = sys.stdin.readline().strip()
    return value if value else default


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(input_folder, output_path, workers=None):
    # 1. Discover and sort MP3s by leading index number
    mp3_files = sorted(
        glob.glob(os.path.join(input_folder, "*.mp3")),
        key=index_sort_key
    )
    if not mp3_files:
        print(f"Error: No MP3 files found in '{input_folder}'")
        sys.exit(1)

    n = len(mp3_files)

    # 2. Scan ALL files: read tags + duration upfront
    print(f"\nScanning {n} MP3 file(s)...")
    tags, artwork = read_mp3_tags(mp3_files[0])
    folder_name = Path(input_folder).resolve().name
    # Parse "Author - Book Title" folder format if present
    folder_author, folder_title = None, folder_name
    if " - " in folder_name:
        parts = folder_name.split(" - ", 1)
        folder_author, folder_title = parts[0].strip(), parts[1].strip()
    book_title = tags.get("album", "").strip() or folder_title
    author     = tags.get("artist", tags.get("album_artist", "")).strip() or folder_author or "Unknown"

    # Detect source channels; encode at 128k to give the AAC encoder enough
    # headroom for a clean MP3→AAC transcode without audible artifacts.
    _, src_channels = get_audio_info(mp3_files[0])
    enc_bitrate  = 128
    enc_channels = src_channels or 1   # fall back to mono if undetectable

    scanned = []   # list of (mp3_path, chapter_title, duration)
    for mp3 in mp3_files:
        stem          = Path(mp3).stem
        chapter_title = get_mp3_title(mp3) or stem
        duration      = get_duration(mp3)
        scanned.append((mp3, chapter_title, duration))

    total_duration = sum(d for _, _, d in scanned)

    # 3. Artwork detection
    if not artwork:
        for pattern in ("cover.jpg","cover.png","folder.jpg","folder.png",
                        "artwork.jpg","artwork.png","*.jpg","*.png"):
            matches = glob.glob(os.path.join(input_folder, pattern))
            if matches:
                with open(matches[0], "rb") as f:
                    data = f.read()
                mime = "image/jpeg" if matches[0].lower().endswith(".jpg") else "image/png"
                artwork = (data, mime)
                break

    # 4. Show full summary for confirmation
    W = 72
    print()
    print("─" * W)
    print(f"  {'#':<5} {'Duration':<10} Chapter title")
    print("─" * W)
    for i, (mp3, chapter_title, duration) in enumerate(scanned):
        print(f"  {i+1:<5} {dur(duration):<10} {chapter_title}")
    print("─" * W)
    print(f"  {'':5} {dur(total_duration):<10} Total ({n} chapters)")
    print()

    # 5. Confirm / edit book-level metadata
    print("── Book metadata ──────────────────────────────────────────")
    print("  (Press Enter to keep, or type a new value)")
    book_title = prompt("Title", book_title)
    author     = prompt("Author", author)
    tags["album"]  = book_title
    tags["artist"] = author

    artwork_label = "none"
    if artwork:
        artwork_label = f"embedded ({artwork[1]})"
    ch_label = "mono" if enc_channels == 1 else "stereo"
    print(f"  {'Year':<8} : {tags.get('year', '—')}")
    print(f"  {'Artwork':<8} : {artwork_label}")
    print(f"  {'Encoding':<8} : {enc_bitrate}k AAC {ch_label}")
    print(f"  {'Output':<8} : {output_path}")

    # 6. Final go / no-go
    restore_terminal()
    sys.stdout.write("\nStart conversion? [Y/n]: ")
    sys.stdout.flush()
    confirm = sys.stdin.readline().strip().lower()
    if confirm and confirm not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    with tempfile.TemporaryDirectory() as tmpdir:

        # 7. Convert each MP3 → M4A (reuse already-scanned data)
        print(f"\n── Step 1/3  Converting {n} MP3s to M4A ──────────────────")
        encoded_files = []
        chapter_marks = []
        current_time = 0.0

        workers = min(workers or os.cpu_count() or 4, n)
        print(f"  (using {workers} parallel workers)")

        # Pre-build output paths so order is guaranteed
        encoded_paths = [os.path.join(tmpdir, f"{i:04d}.m4a") for i in range(n)]

        # Overall bar at position 0; per-file bars below it
        outer = tqdm(
            total=n,
            desc="Overall",
            unit="file",
            ncols=72,
            bar_format="  {desc:<10} |{bar}| {n}/{total} files  [{elapsed}<{remaining}]",
            position=0,
            leave=True,
        )
        outer_lock = threading.Lock()

        def encode_one(args):
            i, (mp3, chapter_title, duration) = args
            slot = (i % workers) + 1   # bar position 1..workers
            ffmpeg_with_progress(
                ["ffmpeg", "-y", "-i", mp3,
                 "-vn", "-c:a", "aac", "-b:a", f"{enc_bitrate}k",
                 "-ac", str(enc_channels),
                 encoded_paths[i]],
                duration_sec=duration,
                desc=f"[{i+1}/{n}] {chapter_title[:24]}",
                position=slot,
            )
            with outer_lock:
                outer.update(1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(encode_one, enumerate(scanned)))

        outer.close()
        # Clear worker bars
        print("\033[{}A\033[J".format(workers), end="", flush=True)

        # Build chapter_marks using actual encoded AAC durations (not MP3 estimates)
        for i, (mp3, chapter_title, scanned_dur) in enumerate(scanned):
            actual_dur = get_duration(encoded_paths[i])
            if actual_dur <= 0:
                print(f"  Warning: could not get duration for {encoded_paths[i]}, using scanned duration")
                actual_dur = scanned_dur
            chapter_marks.append((current_time, chapter_title))
            current_time += actual_dur
            encoded_files.append(encoded_paths[i])

        # 8. Concatenate
        print(f"\n── Step 2/3  Concatenating audio ──────────────────────────")
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w") as f:
            for f_path in encoded_files:
                f.write(f"file '{f_path}'\n")

        raw_m4b = os.path.join(tmpdir, "raw.m4b")
        ffmpeg_with_progress(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", concat_list, "-c", "copy", raw_m4b],
            duration_sec=current_time,
            desc="Merging",
        )

        # 8. Save artwork
        art_path = None
        if artwork:
            art_ext = ".jpg" if "jpeg" in artwork[1] else ".png"
            art_path = os.path.join(tmpdir, f"cover{art_ext}")
            with open(art_path, "wb") as f:
                f.write(artwork[0])

        # 9. ffmetadata (tags + chapters)
        meta_path = os.path.join(tmpdir, "meta.txt")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={book_title}\n")
            f.write(f"artist={author}\n")
            f.write(f"album={book_title}\n")
            f.write(f"album_artist={author}\n")
            if "year" in tags:
                f.write(f"date={tags['year']}\n")
            if "genre" in tags:
                f.write(f"genre={tags['genre']}\n")
            f.write("media_type=2\n\n")
            actual_duration = get_duration(raw_m4b)
            for j, (start, title) in enumerate(chapter_marks):
                end = chapter_marks[j+1][0] if j+1 < len(chapter_marks) else actual_duration
                f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                f.write(f"START={int(start*1000)}\n")
                f.write(f"END={int(end*1000)}\n")
                f.write(f"title={title}\n\n")

        # 10. Final mux
        print(f"\n── Step 3/3  Writing final M4B ────────────────────────────")
        cmd = ["ffmpeg", "-y", "-i", raw_m4b, "-i", meta_path]
        if art_path:
            cmd += ["-i", art_path,
                    "-map", "0:a", "-map", "2:v",
                    "-disposition:v:0", "attached_pic"]
        else:
            cmd += ["-map", "0:a"]
        cmd += ["-map_metadata", "1", "-map_chapters", "1",
                "-c:a", "copy", "-c:v", "copy",
                output_path]

        ffmpeg_with_progress(cmd, duration_sec=current_time, desc="Muxing")

    # Done
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"\n✅  {output_path}")
        print(f"    Size:     {size_mb:.1f} MB")
        print(f"    Chapters: {len(chapter_marks)}")
        print(f"    Duration: {dur(current_time)}")
    else:
        print("\n❌ Output file was not created.")
        sys.exit(1)


# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Convert a folder of MP3s into a single M4B audiobook.",
        usage="mp3_to_m4b.py input_folder [output] [-w N]"
    )
    parser.add_argument("input_folder", help="Folder containing MP3 files")
    parser.add_argument("output", nargs="?", help="Output .m4b path (default: <folder>.m4b)")
    parser.add_argument(
        "-w", "--workers",
        type=int, default=None,
        metavar="N",
        help=f"Parallel encoding workers (default: all CPU cores, currently {os.cpu_count()})"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.input_folder):
        print(f"Error: '{args.input_folder}' is not a directory.")
        sys.exit(1)

    folder = Path(args.input_folder).resolve()
    output_path = args.output or str(folder.parent / f"{folder.name}.m4b")
    convert(args.input_folder, output_path, workers=args.workers)


if __name__ == "__main__":
    main()
