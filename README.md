# mp3-to-m4b

Convert a folder of MP3 files into a single M4B audiobook file for Apple Books.

## Requirements

- Python 3.7+
- [ffmpeg](https://ffmpeg.org/) in your PATH (e.g. `brew install ffmpeg`)
- Python packages are auto-installed on first run (`mutagen`, `tqdm`)

## Usage

```
python mp3_to_m4b.py <input_folder> [output] [-w N]
```

| Argument | Description |
|---|---|
| `input_folder` | Folder containing MP3 files |
| `output` | Output `.m4b` path (default: `<input_folder>.m4b` next to input folder) |
| `-w N`, `--workers N` | Number of parallel encoding workers (default: all CPU cores) |

## Examples

```bash
python mp3_to_m4b.py ~/Downloads/"My Book"
python mp3_to_m4b.py ~/Downloads/"My Book" ~/Desktop/"My Book.m4b"
python mp3_to_m4b.py ~/Downloads/"My Book" -w 6
```

## Features

- **Chapters** — each MP3 becomes one chapter; title taken from the `TIT2` ID3 tag, falls back to the filename stem
- **Book title** — taken from the `TALB` tag, falls back to the folder name
- **Author** — taken from the `TPE1` tag; if missing, parsed from the folder name when it follows the `Author - Book Title` format
- **Folder name formats supported:**
  - `Book Title` — title only
  - `Author Name - Book Title` — author and title parsed automatically
- **Artwork** — embedded from ID3 tags or an image file in the folder (`cover.jpg`, `cover.png`, etc.)
- **Fast encoding** — parallel AAC encoding across all CPU cores
- **Interactive confirmation** — review chapter list and edit title/author before conversion starts

## How It Works

1. **Scan** — Reads ID3 tags and duration from all MP3s; detects cover artwork
2. **Confirm** — Shows chapter list and lets you edit the book title and author
3. **Encode** — Converts each MP3 to AAC (M4A) in parallel using ffmpeg
4. **Concatenate** — Merges all M4A files into one
5. **Mux** — Writes chapter markers, metadata, and artwork into the final `.m4b`

## File Ordering

MP3s are sorted by their leading index number (e.g. `01.mp3`, `02.mp3`). Files without a leading number fall back to alphabetical order.

## Artwork Detection

Cover art is sourced in order of preference:
1. Embedded `APIC` frame in the first MP3's ID3 tags
2. Image file in the input folder: `cover.jpg`, `cover.png`, `folder.jpg`, `folder.png`, `artwork.jpg`, `artwork.png`, or any `*.jpg` / `*.png`
