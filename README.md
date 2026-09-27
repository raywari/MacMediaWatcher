# 🎬 MacMediaWatcher

[![en](https://img.shields.io/badge/lang-English-blue)](README.md) [![ru](https://img.shields.io/badge/lang-Русский-red)](README.ru.md)

Auto-converter for unsupported media formats on macOS, built for video editors.

If you edit video in **DaVinci Resolve**, **Premiere Pro**, **Final Cut Pro**, or any other video editor, you know the pain: you download assets from stock sites like Envato, Freepik, Shutterstock, or Adobe Stock, and instead of usable files you get **AVIF**, **SVG**, **TIFF**, or a **.ZIP** archive with the actual video buried inside alongside junk files. Your editor can't import any of that, so every time you have to manually convert, extract, and clean up.

**MacMediaWatcher** fixes this. It runs in the background and watches your working directories. As soon as an unsupported file appears, it automatically converts or extracts it and deletes the original. You just download from the stock site and the import-ready file is already waiting in your folder.

## What It Does

### Image Conversion

Conversion is powered by **`sips`** (Scriptable Image Processing System) — a utility built into macOS. It ships with every Mac out of the box, no installation required. `sips` handles a wide range of formats and converts them to standard JPEG/PNG without quality loss.

| Format | Output | Command |
|--------|--------|---------|
| `.avif` | `.jpg` | `sips -s format jpeg` |
| `.tif` `.tiff` | `.jpg` | `sips -s format jpeg` |
| `.svg` | `.png` | `sips -s format png -Z 2000` (max side 2000px, configurable) |

The original file is deleted after successful conversion.

### ZIP Extraction

Many stock sites deliver assets in ZIP archives where the actual video is buried deep alongside junk (previews, licenses, readme files, etc.). MacMediaWatcher:

1. Opens the ZIP archive
2. Finds only video files in supported formats (`.mp4`, `.mov`, `.mkv`, `.avi`, `.m4v`, `.webm`, `.prores`)
3. Extracts them to the same directory where the archive was located
4. Ignores system junk (`__MACOSX`, `.DS_Store`)
5. Deletes the ZIP archive

The result: clean video files right next to the rest of your project assets, no extra folders or garbage.

## Requirements

- **macOS** (uses the built-in `sips` utility — does not work on Windows/Linux)
- Python 3.8+

## Installation

```bash
git clone https://github.com/raywari/MacMediaWatcher.git
cd MacMediaWatcher
pip3 install -r requirements.txt
```

## Configuration

On first launch, `config.yaml` is automatically created from the `config.example.yaml` template. Open it and set your paths:

```yaml
watch_dirs:
  - ~/Desktop/Projects
  - /Volumes/SSD/Footage

ignored_dirs:
  - "TEMPLATES"
  - "ASSETS"

# Only process files inside subfolders with this name.
# Leave empty ("") to process everything.
target_folder: "DOWNLOADS"
```

For the full list of settings (threads, timeouts, formats, retries), see [`config.example.yaml`](config.example.yaml).

## Usage

```bash
python3 watcher.py
```

To run automatically at system startup, use `launchd` (macOS) — create a `.plist` file in `~/Library/LaunchAgents/`.

## How It Works

1. On startup, scans all target directories and processes any existing files
2. Starts a `watchdog` observer on each directory
3. New files enter the queue → stability check (waits until the file is fully downloaded) → conversion/extraction
4. Extraction and conversion run in separate threads (up to 4 in parallel, configurable)
5. If a directory becomes unavailable (external drive disconnected), the script waits and automatically reconnects when it reappears
6. If processing fails — up to 3 automatic retries with increasing intervals (30s, 90s)
7. On crash recovery, the script cleans up any leftover temp files from the previous run

## License

MIT
