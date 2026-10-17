"""
Audio Converter - backend
Run:  python app.py
It starts a local server and opens the HTML window (templates/index.html).
"""
import json
import logging
import os
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.serving import make_server

def resource_path(*parts):
    """Find bundled files both when running as a script and as a packaged .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


TEMPLATES = resource_path("templates")

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac",
    ".ogg", ".opus", ".wma", ".aiff", ".aif",
    ".ape", ".alac", ".mp4", ".webm",
}

app = Flask(__name__, static_folder=None)
TEMP_DIR = tempfile.mkdtemp(prefix="audio_converter_")
jobs = {}
active_procs = {}                 # running ffmpeg processes -> their output file
active_lock = threading.Lock()

# Hide the black console window that would flash on Windows.
NO_WINDOW = {}
if os.name == "nt":
    NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW}


# ---------------------------------------------------------------- helpers
def default_output_folder():
    return os.path.join(os.path.expanduser("~"), "Music", "Converted Audio")


def clean_name(name):
    name = os.path.basename(name or "audio")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def unique_path(folder, stem):
    path = os.path.join(folder, stem + ".mp3")
    n = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({n}).mp3")
        n += 1
    return path


def probe(source):
    """Return (codec_name, duration_seconds) of the first audio stream."""
    codec_run = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", source],
        capture_output=True, text=True, check=True, **NO_WINDOW,
    )
    lines = codec_run.stdout.strip().splitlines()
    codec = lines[0] if lines else ""

    duration = 0.0
    dur_run = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", source],
        capture_output=True, text=True, **NO_WINDOW,
    )
    try:
        duration = float(dur_run.stdout.strip())
    except ValueError:
        pass
    return codec, duration


def convert_one(item, bitrate):
    codec, duration = probe(item["src"])
    if not codec:
        raise RuntimeError("No audio stream found in this file.")

    command = ["ffmpeg", "-y", "-nostdin", "-i", item["src"],
               "-map", "0:a:0", "-vn"]
    if codec == "mp3":
        command += ["-c:a", "copy"]          # keep existing MP3 data
    else:
        command += ["-c:a", "libmp3lame", "-b:a", bitrate]
    command += ["-map_metadata", "0",
                "-progress", "pipe:1", "-nostats", item["dest"]]

    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )

    with active_lock:
        active_procs[proc] = item["dest"]

    tail = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            value = line.split("=", 1)[1]
            if value.lstrip("-").isdigit() and duration > 0:
                seconds = int(value) / 1_000_000
                item["percent"] = max(0, min(99, int(seconds / duration * 100)))
        elif "=" not in line and line:
            tail.append(line)
            tail = tail[-12:]

    proc.wait()
    with active_lock:
        active_procs.pop(proc, None)
    if proc.returncode != 0:
        if os.path.exists(item["dest"]):
            os.remove(item["dest"])
        raise RuntimeError(" ".join(tail)[-300:] or "Conversion failed.")


def run_job(job_id, bitrate):
    job = jobs[job_id]
    for item in job["files"]:
        if item["status"] == "error":
            continue
        item["status"] = "converting"
        try:
            convert_one(item, bitrate)
            item["percent"] = 100
            item["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            item["status"] = "error"
            item["error"] = str(exc)
        finally:
            try:
                os.remove(item["src"])
            except OSError:
                pass
    job["done"] = True


# ----------------------------------------------------------------- routes
@app.route("/")
def index():
    return send_from_directory(TEMPLATES, "index.html")


@app.route("/api/config")
def config():
    return jsonify(
        folder=default_output_folder(),
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
    )


@app.route("/api/choose-folder", methods=["POST"])
def choose_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Choose output folder")
        root.destroy()
        return jsonify(folder=folder or "")
    except Exception as exc:  # noqa: BLE001
        return jsonify(folder="", error=str(exc)), 500


@app.route("/api/convert", methods=["POST"])
def convert():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return jsonify(error="FFmpeg not found. Install it and add it to PATH."), 400

    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="No files received."), 400

    bitrate = request.form.get("bitrate", "320k")
    if bitrate not in {"320k", "256k", "192k", "128k"}:
        bitrate = "320k"

    folder = (request.form.get("folder") or "").strip() or default_output_folder()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return jsonify(error=f"Cannot use that folder: {exc}"), 400

    job_id = uuid.uuid4().hex
    files = []
    for upload in uploads:
        name = clean_name(upload.filename)
        stem, ext = os.path.splitext(name)
        item = {"name": name, "percent": 0, "status": "queued",
                "error": "", "src": "", "dest": ""}

        if ext.lower() not in AUDIO_EXTENSIONS:
            item["status"] = "error"
            item["error"] = "Unsupported file type."
        else:
            item["src"] = os.path.join(TEMP_DIR, uuid.uuid4().hex + ext.lower())
            upload.save(item["src"])
            item["dest"] = unique_path(folder, stem)
        files.append(item)

    jobs[job_id] = {"files": files, "done": False, "folder": folder}
    threading.Thread(target=run_job, args=(job_id, bitrate), daemon=True).start()
    return jsonify(job=job_id)


@app.route("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404

    files = job["files"]
    overall = int(sum(f["percent"] for f in files) / len(files)) if files else 0
    return jsonify(
        done=job["done"],
        percent=100 if job["done"] else overall,
        folder=job["folder"],
        files=[{"name": f["name"], "percent": f["percent"],
                "status": f["status"], "error": f["error"]} for f in files],
    )


@app.route("/api/download/<job_id>/<int:index>")
def download(job_id, index):
    job = jobs.get(job_id)
    if not job or index >= len(job["files"]):
        return jsonify(error="Not found"), 404
    item = job["files"][index]
    if item["status"] != "done" or not os.path.isfile(item["dest"]):
        return jsonify(error="File not ready"), 404
    return send_file(item["dest"], as_attachment=True)


@app.route("/api/reveal/<job_id>/<int:index>", methods=["POST"])
def reveal(job_id, index):
    """Open File Explorer with the converted file selected."""
    job = jobs.get(job_id)
    if not job or index >= len(job["files"]):
        return jsonify(error="Not found"), 404
    item = job["files"][index]
    if not os.path.isfile(item["dest"]):
        return jsonify(error="File not found"), 404
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(item["dest"])])
    return jsonify(ok=True)


# ============================================================ folder watcher
# Settings and the activity log live in %APPDATA%\AudioConverter
APPDATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AudioConverter")
CONFIG_PATH = os.path.join(APPDATA, "config.json")
LOG_PATH = os.path.join(APPDATA, "watch.log")
WATCH_LOCK_PORT = 47653          # only one copy of the app may watch at a time

# Videos are skipped on purpose so movies in your folder are never turned into MP3s.
WATCH_EXTENSIONS = AUDIO_EXTENSIONS - {".mp4", ".webm"}

DEFAULT_CONFIG = {
    "watch_folder": "",
    "watch_enabled": False,
    "delete_original": False,
    "convert_existing": False,
    "bitrate": "320k",
    "baseline_folder": "",
    "processed": {},
}

config_lock = threading.Lock()
watch_thread = None
watch_stop = threading.Event()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not isinstance(cfg.get("processed"), dict):
        cfg["processed"] = {}
    return cfg


def save_config(cfg):
    os.makedirs(APPDATA, exist_ok=True)
    if len(cfg["processed"]) > 5000:           # keep the file small
        keys = list(cfg["processed"])[-3000:]
        cfg["processed"] = {k: cfg["processed"][k] for k in keys}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def log(text, kind="info"):
    try:
        os.makedirs(APPDATA, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}\t{kind}\t{text}\n")
    except OSError:
        pass


_last_warn = {}


def warn_once(text, every=600):
    """Log a problem at most once every `every` seconds (avoids log spam)."""
    now = time.time()
    if now - _last_warn.get(text, 0) > every:
        _last_warn[text] = now
        log(text, "err")


def kill_active():
    """Stop any running conversion and remove its half-written file."""
    with active_lock:
        procs = list(active_procs.items())
    for proc, dest in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


def read_log(count=40):
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()[-count:]
    except OSError:
        return []
    entries = []
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) == 3:
            entries.append({"time": parts[0], "kind": parts[1], "text": parts[2]})
    return entries[::-1]                        # newest first


def trim_log():
    try:
        if os.path.getsize(LOG_PATH) > 300_000:
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()[-200:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def signature(path):
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}", st


def mark_processed(*paths):
    with config_lock:
        cfg = load_config()
        for p in paths:
            try:
                cfg["processed"][p] = signature(p)[0]
            except OSError:
                pass
        save_config(cfg)


def iter_audio(folder):
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if os.path.splitext(name)[1].lower() in WATCH_EXTENSIONS:
                yield os.path.join(root, name)


def handle_file(path, cfg):
    name = os.path.basename(path)
    folder = os.path.dirname(path)
    stem, ext = os.path.splitext(name)
    is_mp3_name = ext.lower() == ".mp3"

    try:
        codec, _ = probe(path)
    except FileNotFoundError:
        warn_once("FFmpeg not found. Install it and add it to PATH.")
        return                     # not marked as done, so it is retried later
    except Exception:  # noqa: BLE001
        log(f"Could not read {name}. It will be retried if it changes.", "err")
        mark_processed(path)
        return

    if codec == "mp3":
        log(f"{name} is already MP3. Nothing to do.", "info")
        mark_processed(path)
        return
    if not codec:
        log(f"{name} has no audio stream. Skipped.", "err")
        mark_processed(path)
        return
    if not is_mp3_name and os.path.exists(os.path.join(folder, stem + ".mp3")):
        log(f"{name}: an MP3 with the same name already exists. Skipped.", "info")
        mark_processed(path)
        return

    dest = unique_path(folder, stem)
    item = {"src": path, "dest": dest, "percent": 0}
    log(f"Converting {name} ...")
    try:
        convert_one(item, cfg.get("bitrate", "320k"))
    except Exception as exc:  # noqa: BLE001
        log(f"Failed to convert {name}: {exc}", "err")
        mark_processed(path)
        return

    mark_processed(path, dest)
    out_name = os.path.basename(dest)

    # Never delete a file that was named .mp3 (it is kept as the original).
    if cfg.get("delete_original") and not is_mp3_name:
        try:
            os.remove(path)
            log(f"Converted {name} to {out_name}. Original deleted.", "ok")
        except OSError as exc:
            log(f"Converted {name} to {out_name}, but could not delete it: {exc}", "err")
    else:
        log(f"Converted {name} to {out_name}.", "ok")


def scan_folder(folder, pending):
    cfg = load_config()
    processed = cfg["processed"]

    # First time we see this folder: ignore what is already inside it
    # (unless "convert existing" is on). Only NEW downloads are handled.
    if cfg.get("baseline_folder") != folder:
        count = 0
        if not cfg.get("convert_existing"):
            for path in iter_audio(folder):
                try:
                    processed[path] = signature(path)[0]
                    count += 1
                except OSError:
                    pass
            log(f"Watching {folder}. Ignoring {count} audio file(s) already there.")
        else:
            log(f"Watching {folder}. Existing files will be checked too.")
        with config_lock:
            fresh = load_config()
            fresh["processed"].update(processed)
            fresh["baseline_folder"] = folder
            save_config(fresh)
        return 1 if cfg.get("convert_existing") else 0

    waiting = 0
    for path in iter_audio(folder):
        if watch_stop.is_set():
            return 0
        try:
            sig, st = signature(path)
        except OSError:
            continue
        if processed.get(path) == sig:
            continue

        # Wait until the download has finished: the file must stop changing.
        key = (st.st_size, st.st_mtime)
        if time.time() - st.st_mtime < 3 or pending.get(path) != key:
            pending[path] = key
            waiting += 1
            continue
        pending.pop(path, None)
        handle_file(path, cfg)

    return waiting


def acquire_watch_lock():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", WATCH_LOCK_PORT))
        return lock
    except OSError:
        lock.close()
        return None


def run_scan_once(max_wait=90):
    """Check the folder, convert what is new, then return (the app then quits)."""
    cfg = load_config()
    folder = cfg.get("watch_folder", "")
    if not (cfg.get("watch_enabled") and folder and os.path.isdir(folder)):
        return
    lock = acquire_watch_lock()
    if not lock:
        return                         # the open window is already watching
    try:
        pending = {}
        deadline = time.time() + max_wait
        while True:
            waiting = scan_folder(folder, pending)
            if not waiting or time.time() > deadline:
                break                  # nothing left, or finish on the next run
            time.sleep(3)
    finally:
        lock.close()


def watcher_loop():
    lock = acquire_watch_lock()
    if not lock:
        log("Another copy of Audio Converter is already watching.", "info")
        return

    pending = {}
    try:
        while not watch_stop.is_set():
            cfg = load_config()
            folder = cfg.get("watch_folder", "")
            if cfg.get("watch_enabled") and folder and os.path.isdir(folder):
                try:
                    scan_folder(folder, pending)
                except Exception as exc:  # noqa: BLE001
                    log(f"Watcher error: {exc}", "err")
            watch_stop.wait(3)
    finally:
        lock.close()


def start_watcher():
    global watch_thread
    watch_stop.clear()
    if watch_thread and watch_thread.is_alive():
        return
    watch_thread = threading.Thread(target=watcher_loop, daemon=True)
    watch_thread.start()


def stop_watcher():
    watch_stop.set()


def watcher_state(cfg):
    if not cfg.get("watch_enabled"):
        return "off"
    if watch_thread and watch_thread.is_alive() and not watch_stop.is_set():
        return "on"
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe_sock.bind(("127.0.0.1", WATCH_LOCK_PORT))
        return "off"                  # nobody is holding the lock
    except OSError:
        return "background"           # another copy (background mode) is watching
    finally:
        probe_sock.close()


@app.route("/api/watch", methods=["GET", "POST"])
def watch_api():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        folder = (data.get("folder") or "").strip()
        enabled = bool(data.get("enabled"))
        if enabled and not os.path.isdir(folder):
            return jsonify(error="Choose an existing folder to watch first."), 400

        bitrate = data.get("bitrate", "320k")
        if bitrate not in {"320k", "256k", "192k", "128k"}:
            bitrate = "320k"

        with config_lock:
            cfg = load_config()
            convert_existing = bool(data.get("convert_existing"))
            if folder != cfg["watch_folder"] or convert_existing != cfg["convert_existing"]:
                cfg["baseline_folder"] = ""          # take a fresh look at the folder
                if convert_existing:
                    cfg["processed"] = {}
            cfg.update(
                watch_folder=folder,
                watch_enabled=enabled,
                delete_original=bool(data.get("delete_original")),
                convert_existing=convert_existing,
                bitrate=bitrate,
            )
            save_config(cfg)

        if enabled:
            start_watcher()
        else:
            stop_watcher()
            log("Watching turned off.")

    cfg = load_config()
    return jsonify(
        folder=cfg["watch_folder"],
        enabled=cfg["watch_enabled"],
        delete_original=cfg["delete_original"],
        convert_existing=cfg["convert_existing"],
        bitrate=cfg["bitrate"],
        state=watcher_state(cfg),
        log=read_log(40),
    )


TASK_NAME = "Audio Converter Auto"


def task_command():
    if getattr(sys, "frozen", False):                 # the packaged .exe
        return f'"{sys.executable}" --scan'
    exe = sys.executable                              # running as a script
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw                                     # no console window
    return f'"{exe}" "{os.path.abspath(__file__)}" --scan'


def run_schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, **NO_WINDOW)


def schedule_exists():
    if os.name != "nt":
        return False
    try:
        return run_schtasks("/Query", "/TN", TASK_NAME).returncode == 0
    except OSError:
        return False


@app.route("/api/schedule", methods=["GET", "POST"])
def schedule_api():
    if request.method == "POST":
        if os.name != "nt":
            return jsonify(error="Scheduling is only available on Windows."), 400
        data = request.get_json(force=True, silent=True) or {}
        try:
            if data.get("enabled"):
                minutes = min(60, max(1, int(data.get("minutes", 2))))
                result = run_schtasks("/Create", "/TN", TASK_NAME, "/SC", "MINUTE",
                                      "/MO", str(minutes), "/TR", task_command(), "/F")
                if result.returncode != 0:
                    msg = (result.stderr or result.stdout or "").strip()
                    return jsonify(error=f"Windows refused to create the task. {msg}"[:300]), 400
                log(f"Background check turned on (every {minutes} min).")
            else:
                run_schtasks("/Delete", "/TN", TASK_NAME, "/F")
                log("Background check turned off.")
        except OSError as exc:
            return jsonify(error=f"Could not run schtasks: {exc}"), 400
    return jsonify(enabled=schedule_exists())


# ------------------------------------------------------------------- main
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    trim_log()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # Scan mode (started by the Windows scheduled task):
    # check the folder, convert new files, then quit by itself.
    if "--scan" in sys.argv:
        try:
            run_scan_once()
        finally:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
        os._exit(0)

    # Background mode: no window, keeps watching until stopped in Task Manager.
    if "--background" in sys.argv:
        start_watcher()
        while watch_thread.is_alive():
            time.sleep(2)
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        os._exit(0)

    import webview  # pip install pywebview

    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if load_config()["watch_enabled"]:
        start_watcher()          # watch while the window is open

    webview.create_window(
        "Audio Converter",
        f"http://127.0.0.1:{port}",
        width=720,
        height=980,
        min_size=(520, 640),
    )
    webview.start()              # blocks until the window is closed

    # Close button clicked: stop everything and quit completely.
    stop_watcher()
    kill_active()
    server.shutdown()
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os._exit(0)
