"""
FFmpeg Video Compilation Service
Receives video URLs, concatenates them with FFmpeg, uploads to Cloudflare R2
via the media-onedrive-proxy Worker.

POST /compile          ÔåÆ returns job_id immediately (async)
GET  /status/<job_id>  ÔåÆ progress + R2 URL when done
"""

import os
import subprocess
import tempfile
import logging
import threading
import uuid
import time
from flask import Flask, request, jsonify
import requests as http_requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "change-me-in-production")
WORKER_UPLOAD_URL = os.environ.get(
    "WORKER_UPLOAD_URL",
    "https://media-onedrive-proxy.yellow-dust-f7b9.workers.dev/upload",
)
WORKER_UPLOAD_SECRET = os.environ.get("WORKER_UPLOAD_SECRET", "")

jobs: dict = {}
JOBS_MAX_AGE = 7200  # auto-clean jobs older than 2 hours


def _cleanup_old_jobs():
    now = time.time()
    stale = [jid for jid, j in jobs.items() if now - j["created"] > JOBS_MAX_AGE]
    for jid in stale:
        del jobs[jid]


def require_api_key(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)

    return decorated


def _compile_worker(job_id: str, video_urls: list, r2_key: str):
    job = jobs[job_id]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Download all source videos
            downloaded = []
            job["step"] = "downloading"
            for i, url in enumerate(video_urls):
                filepath = os.path.join(tmpdir, f"video_{i:03d}.mp4")
                logger.info(f"[{job_id}] Downloading {i+1}/{len(video_urls)}")
                job["progress"] = f"Downloading {i+1}/{len(video_urls)}"
                try:
                    r = http_requests.get(url, stream=True, timeout=120)
                    r.raise_for_status()
                    with open(filepath, "wb") as vf:
                        for chunk in r.iter_content(chunk_size=65536):
                            vf.write(chunk)
                    downloaded.append(filepath)
                except Exception as e:
                    logger.error(f"[{job_id}] Failed to download video {i}: {e}")
                    continue

            if len(downloaded) < 2:
                job["status"] = "error"
                job["error"] = f"Only {len(downloaded)} videos downloaded, need at least 2"
                return

            output_path = os.path.join(tmpdir, "compilation.mp4")

            # 2. Normalize each clip individually (low RAM usage ÔÇö 1 file at a time)
            #    Required because clips have heterogeneous codecs/resolutions/fps,
            #    and stream-copy concat produces corrupt frames in that case.
            job["step"] = "normalizing"
            normalized = []
            for i, fp in enumerate(downloaded):
                out = os.path.join(tmpdir, f"normalized_{i:03d}.mp4")
                job["progress"] = f"Normalizing {i+1}/{len(downloaded)}"
                logger.info(f"[{job_id}] Normalizing {i+1}/{len(downloaded)}")
                cmd_norm = [
                    "ffmpeg", "-i", fp,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
                    "-vf",
                    "scale=720:1280:force_original_aspect_ratio=decrease,"
                    "pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                    "-r", "30",
                    "-threads", "1",
                    "-x264-params", "ref=1:bframes=0:rc-lookahead=10",
                    "-y", out,
                ]
                result = subprocess.run(cmd_norm, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    logger.error(f"[{job_id}] Normalize {i} failed: {result.stderr[-300:]}")
                    continue
                normalized.append(out)

            if len(normalized) < 2:
                job["status"] = "error"
                job["error"] = f"Only {len(normalized)} videos normalized successfully"
                return

            # 3. Concat with stream-copy (no re-encoding = very low memory)
            file_list = os.path.join(tmpdir, "files.txt")
            with open(file_list, "w") as f:
                for fp in normalized:
                    f.write(f"file '{fp}'\n")

            job["step"] = "compiling"
            job["progress"] = f"Concat {len(normalized)} normalized clips..."
            cmd_concat = [
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", file_list,
                "-c", "copy",
                "-t", "570",
                "-movflags", "+faststart",
                "-y", output_path,
            ]
            result = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                logger.error(f"[{job_id}] FFmpeg concat failed: {result.stderr[-500:]}")
                job["status"] = "error"
                job["error"] = f"FFmpeg concat failed: {result.stderr[-300:]}"
                return

            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
                job["status"] = "error"
                job["error"] = "Output file is empty or missing"
                return

            output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"[{job_id}] Compilation successful: {output_size_mb:.1f}MB")

            # 4. Upload to R2 via Worker
            job["step"] = "uploading"
            job["progress"] = f"Uploading {output_size_mb:.0f}MB to R2..."
            try:
                with open(output_path, "rb") as f:
                    r = http_requests.post(
                        f"{WORKER_UPLOAD_URL}?key={r2_key}",
                        headers={
                            "Authorization": f"Bearer {WORKER_UPLOAD_SECRET}",
                            "Content-Type": "video/mp4",
                        },
                        data=f,
                        timeout=900,
                    )
                r.raise_for_status()
                upload_response = r.json()
            except Exception as e:
                logger.error(f"[{job_id}] R2 upload failed: {e}")
                job["status"] = "error"
                job["error"] = f"R2 upload failed: {str(e)}"
                return

            logger.info(f"[{job_id}] Upload complete: {upload_response['url']}")

            job["status"] = "done"
            job["step"] = "done"
            job["progress"] = "Complete"
            job["result"] = {
                "url": upload_response["url"],
                "key": upload_response["key"],
                "size_mb": round(output_size_mb, 1),
                "videos_compiled": len(downloaded),
            }

    except Exception as e:
        logger.exception(f"[{job_id}] Unexpected error in compile worker")
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/health", methods=["GET"])
def health():
    _cleanup_old_jobs()
    active = sum(1 for j in jobs.values() if j["status"] == "processing")
    return jsonify({
        "status": "ok",
        "active_jobs": active,
        "total_jobs": len(jobs),
        "worker_configured": bool(WORKER_UPLOAD_SECRET),
    })


@app.route("/compile", methods=["POST"])
@require_api_key
def compile_videos():
    """
    POST /compile
    JSON body:
    {
        "urls": ["https://...", ...],
        "key":  "car-weekly-2026-05-04.mp4"
    }
    """
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    video_urls = data.get("urls", [])
    r2_key = data.get("key")

    if not video_urls:
        return jsonify({"error": "No video URLs provided"}), 400
    if len(video_urls) < 2:
        return jsonify({"error": "Need at least 2 videos to compile"}), 400
    if not r2_key:
        return jsonify({"error": "Missing 'key' parameter (R2 object key)"}), 400
    if not WORKER_UPLOAD_SECRET:
        return jsonify({"error": "Server misconfigured: WORKER_UPLOAD_SECRET not set"}), 500

    _cleanup_old_jobs()

    job_id = uuid.uuid4().hex[:16]
    jobs[job_id] = {
        "status": "processing",
        "step": "queued",
        "progress": "Starting...",
        "created": time.time(),
        "videos": len(video_urls),
        "result": None,
        "error": None,
    }

    logger.info(f"[{job_id}] Job created ÔÇö {len(video_urls)} videos ÔåÆ {r2_key}")

    thread = threading.Thread(
        target=_compile_worker,
        args=(job_id, video_urls, r2_key),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "processing",
        "videos": len(video_urls),
        "message": "Compilation started",
    }), 202


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found", "job_id": job_id}), 404

    response = {
        "job_id": job_id,
        "status": job["status"],
        "step": job.get("step", ""),
        "progress": job.get("progress", ""),
        "videos": job.get("videos", 0),
        "elapsed": round(time.time() - job["created"], 1),
    }

    if job["status"] == "done" and job["result"]:
        response.update(job["result"])
    elif job["status"] == "error":
        response["error"] = job["error"]

    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
