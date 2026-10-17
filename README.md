
# Audio Converter

A small Windows app that converts audio files to MP3. It has an Apple-inspired interface, drag and drop, a live progress bar with percentage, and an optional folder watcher that converts new downloads automatically.

Built with Python (Flask), FFmpeg, and a plain HTML/CSS/JS interface shown in a native window through [pywebview](https://pywebview.flowrl.com/).

## Features

**Manual mode**
- Drag and drop files, or use **Choose files** / **Choose folder**
- Supports MP3, WAV, FLAC, M4A, AAC, OGG, OPUS, WMA, AIFF, APE, ALAC, MP4 and WebM
- Real progress: a percentage per file and an overall bar
- Bitrate choice: 320k, 256k, 192k or 128k
- Files that are already MP3 are copied without re-encoding
- Metadata is kept, and original files are never overwritten (`Song (1).mp3` is used if a name is taken)
- "Show in folder" button after each conversion
- Light and dark mode follow your Windows setting

**Auto-convert mode**
- Watches a folder (including subfolders) and checks each new audio file
- Waits until a download has finished before touching it
- If the file is already MP3, nothing happens; otherwise it is converted to MP3 in the same folder
- Optional: delete the original after converting (off by default)
- Optional: also convert files that were already in the folder (off by default)
- Optional: keep converting while the app is closed. This creates a Windows scheduled task that starts the app briefly every 2 minutes, converts anything new, and quits by itself
- Activity log inside the app
- Video files (`.mp4`, `.webm`) are ignored by the watcher so movies are never turned into MP3s

Closing the window quits the app completely and cancels any running conversion.

## Requirements

- Windows 10 or 11 (uses the Edge WebView2 runtime, included with current Windows)
- Python 3.9 or newer
- [FFmpeg](https://ffmpeg.org/) with `ffmpeg` and `ffprobe` on your `PATH`

Install FFmpeg on Windows with:

```
winget install Gyan.FFmpeg
```

Then open a new terminal and check:

```
ffmpeg -version
ffprobe -version
```

## Run from source

```
pip install -r requirements.txt
python app.py
```

If you use `uv`: `uv pip install -r requirements.txt`

## Build a standalone .exe

```
pip install pyinstaller
python -m PyInstaller --noconsole --onefile --name "Audio Converter" --add-data "templates;templates" app.py
```

Or double-click `build.bat`. The app is created at `dist\Audio Converter.exe`. FFmpeg is not bundled, so it still needs to be installed on the PC.

## Project layout

```
.
├── app.py              Flask backend, FFmpeg runner, folder watcher, native window
├── templates/
│   └── index.html      The whole interface (HTML, CSS and JavaScript)
├── requirements.txt
├── build.bat           One-click .exe build
└── LICENSE
```

## How it works

1. `app.py` starts a small local web server on a free port and opens it in a pywebview window.
2. The page uploads files to the server, which runs FFmpeg with `-progress` to get real timing, and the page polls for progress.
3. Settings and the activity log are stored in `%APPDATA%\AudioConverter`.
4. Command-line modes used by Windows tasks:
   - `--scan` checks the watched folder once, converts new files, then exits
   - `--background` watches with no window until stopped in Task Manager

## Troubleshooting

| Problem | Fix |
|---|---|
| "FFmpeg not found" | Install FFmpeg, add it to `PATH`, then open a new terminal |
| Page shows "Not Found" | `index.html` must be inside the `templates` folder next to `app.py` |
| Background check does nothing on a laptop | In Task Scheduler, open "Audio Converter Auto" → Conditions → untick "Start only if on AC power" |
| Turn off the background check | Use the switch in the app, or run `schtasks /Delete /TN "Audio Converter Auto" /F` |

## License

MIT. See [LICENSE](LICENSE).
