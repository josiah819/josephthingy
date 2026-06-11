"""MediaGrab — self-hosted social media downloader.

Paste a URL (YouTube, TikTok, Instagram, Reddit, Twitter/X, ... anything
yt-dlp supports), get back an MP4 at max resolution or an MP3, delivered
as a browser download.

Single gunicorn worker process required: job state lives in memory.
"""

import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/config/cookies.txt"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
CLEANUP_HOURS = float(os.environ.get("CLEANUP_HOURS", "3"))

QUALITIES = {"max", "2160", "1440", "1080", "720", "480"}
ACTIVE_STATES = {"queued", "starting", "downloading", "processing"}
SKIP_EXT = {".part", ".ytdl", ".temp", ".tmp", ".webp", ".jpg", ".png", ".json"}

PERCENT_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
DEST_RE = re.compile(r"\[download\] Destination: (.+)")
POSTPROC_PREFIXES = (
    "[Merger]", "[ExtractAudio]", "[VideoRemuxer]", "[VideoConvertor]",
    "[Metadata]", "[EmbedThumbnail]", "[ThumbnailsConvertor]", "[Fixup",
)

app = Flask(__name__, static_folder="static")

jobs = {}
lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
_meta_cache = {}


def build_cmd(job):
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "--color", "never",
        "--concurrent-fragments", "4",
        "--no-simulate",
        "--print", "after_move:filepath",
        "-P", str(job["dir"]),
        "-o", "%(title).180B [%(id)s].%(ext)s",
    ]
    if COOKIES_FILE.is_file():
        cmd += ["--cookies", str(COOKIES_FILE)]
    if job["format"] == "mp3":
        cmd += [
            "-f", "ba/b",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-metadata", "--embed-thumbnail",
        ]
    else:
        q = job["quality"]
        if q == "max":
            # m4a audio first so the merged file stays mp4-compatible
            cmd += ["-f", "bv*+ba[ext=m4a]/bv*+ba/b"]
        else:
            cmd += ["-f", f"bv*[height<={q}]+ba[ext=m4a]/bv*[height<={q}]+ba/b[height<={q}]"]
        cmd += ["--merge-output-format", "mp4", "--remux-video", "mp4", "--embed-metadata"]
        if job["compat"]:
            cmd += ["-S", "vcodec:h264,res,acodec:m4a"]
    cmd += ["--", job["url"]]
    return cmd


def find_output(d):
    best = None
    for f in d.iterdir():
        if f.is_file() and f.suffix.lower() not in SKIP_EXT:
            if best is None or f.stat().st_size > best.stat().st_size:
                best = f
    return best


def run_job(job):
    with lock:
        if job["status"] == "canceled":
            return
        job["status"] = "starting"
    job["dir"].mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            build_cmd(job),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
            cwd=str(job["dir"]), start_new_session=True,
        )
    except OSError as e:
        with lock:
            job["status"] = "error"
            job["error"] = f"failed to launch yt-dlp: {e}"
            job["finished"] = time.time()
        return

    with lock:
        if job["status"] == "canceled":
            _kill(proc)
        else:
            job["proc"] = proc
            job["status"] = "downloading"

    jobdir_prefix = str(job["dir"]) + os.sep
    filepath = None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        with lock:
            job["log"].append(line)
            job["detail"] = line[:300]
            m = PERCENT_RE.search(line)
            if m:
                job["progress"] = min(100.0, float(m.group(1)))
                if job["status"] == "downloading":
                    pass
            dm = DEST_RE.search(line)
            if dm and not job.get("title"):
                base = os.path.basename(dm.group(1))
                title = re.sub(r"\s*\[[^\]]+\]\.[A-Za-z0-9]+$", "", base)
                if title:
                    job["title"] = title
            if line.startswith(POSTPROC_PREFIXES):
                job["status"] = "processing"
                job["progress"] = 100.0
            if line.startswith(jobdir_prefix):
                filepath = line

    code = proc.wait()
    with lock:
        job["proc"] = None
        job["finished"] = time.time()
        if job["status"] == "canceled":
            shutil.rmtree(job["dir"], ignore_errors=True)
            return
        if code == 0:
            f = Path(filepath) if filepath and Path(filepath).is_file() else find_output(job["dir"])
            if f is not None:
                job["filepath"] = str(f)
                job["filename"] = f.name
                job["size"] = f.stat().st_size
                job["title"] = re.sub(r"\s*\[[^\]]+\]$", "", f.stem) or job.get("title")
                job["progress"] = 100.0
                job["status"] = "done"
            else:
                job["status"] = "error"
                job["error"] = "yt-dlp finished but no output file was found"
        else:
            err = next((l for l in reversed(job["log"]) if l.startswith("ERROR")), None)
            job["status"] = "error"
            job["error"] = err or f"yt-dlp exited with code {code}"


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass


def public(job):
    out = {k: job.get(k) for k in (
        "id", "url", "status", "progress", "title", "filename",
        "size", "error", "detail", "format", "quality", "created",
    )}
    if job["status"] == "error":
        out["log_tail"] = list(job["log"])[-15:]
    return out


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True, force=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("format", "mp4")
    quality = str(data.get("quality", "max"))
    compat = bool(data.get("compat"))
    if not re.match(r"^https?://\S+$", url) or len(url) > 2000:
        return jsonify({"error": "Enter a valid http(s) link."}), 400
    if fmt not in ("mp4", "mp3") or quality not in QUALITIES:
        return jsonify({"error": "Invalid format or quality."}), 400

    job_id = secrets.token_hex(8)
    job = {
        "id": job_id, "url": url, "format": fmt, "quality": quality,
        "compat": compat, "status": "queued", "progress": 0.0,
        "title": None, "filename": None, "filepath": None, "size": None,
        "error": None, "detail": None, "created": time.time(),
        "finished": None, "proc": None, "dir": DATA_DIR / f"job-{job_id}",
        "log": deque(maxlen=60),
    }
    with lock:
        jobs[job_id] = job
    pool.submit(run_job, job)
    return jsonify(public(job)), 201


@app.get("/api/jobs")
def list_jobs():
    with lock:
        out = [public(j) for j in jobs.values()]
    out.sort(key=lambda j: j["created"], reverse=True)
    return jsonify(out)


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        return jsonify(public(job))


@app.get("/api/jobs/<job_id>/file")
def get_file(job_id):
    with lock:
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        if job["status"] != "done" or not job.get("filepath"):
            abort(409)
        path, name = job["filepath"], job["filename"]
    if not os.path.isfile(path):
        abort(410)
    return send_file(path, as_attachment=True, download_name=name, conditional=True)


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    proc = None
    with lock:
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        if job["status"] in ACTIVE_STATES:
            # cancel: worker thread cleans up when the process dies
            job["status"] = "canceled"
            proc = job.get("proc")
        else:
            jobs.pop(job_id)
            shutil.rmtree(job["dir"], ignore_errors=True)
    if proc is not None and proc.poll() is None:
        _kill(proc)
    return "", 204


@app.get("/api/meta")
def meta():
    if not _meta_cache:
        try:
            v = subprocess.run(["yt-dlp", "--version"], capture_output=True,
                               text=True, timeout=15).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            v = "unknown"
        _meta_cache.update({"ytdlp": v, "cleanup_hours": CLEANUP_HOURS,
                            "cookies": COOKIES_FILE.is_file()})
    _meta_cache["cookies"] = COOKIES_FILE.is_file()
    return jsonify(_meta_cache)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/")
def index():
    return app.send_static_file("index.html")


def janitor():
    while True:
        time.sleep(600)
        cutoff = time.time() - CLEANUP_HOURS * 3600
        with lock:
            stale = [jid for jid, j in jobs.items()
                     if j["status"] not in ACTIVE_STATES
                     and (j.get("finished") or j["created"]) < cutoff]
            removed = [jobs.pop(jid) for jid in stale]
            known = {str(j["dir"]) for j in jobs.values()}
        for j in removed:
            shutil.rmtree(j["dir"], ignore_errors=True)
        try:
            for child in DATA_DIR.iterdir():
                if child.is_dir() and str(child) not in known:
                    shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def _startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # job state is memory-only; anything on disk at boot is an orphan
    for child in DATA_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    threading.Thread(target=janitor, daemon=True).start()


_startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
