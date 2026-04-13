"""
FFmpeg Video Compilation Service
Receives video URLs, concatenates them with FFmpeg, uploads to Cloudinary.
Designed for Make.com integration (weekly/monthly video compilations).

v2 — Async mode: POST /compile returns a job_id immediately.
      GET /status/<job_id> returns progress and result URL when done.
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
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "change-me-in-production")

# ---------------------------------------------------------------------------
# In-memory job store  (single gunicorn worker → safe)
# ---------------------------------------------------------------------------
jobs: dict = {}
JOBS_MAX_AGE = 7200  # auto-clean jobs older than 2 hours


def _cleanup_old_jobs():
    """Remove jobs older than JOBS_MAX_AGE seconds."""
    now = time.time()
    stale = [jid for jid, j in jobs.items() if now - j["created"] > JOBS_MAX_AGE]
    for jid in stale:
        del jobs[jid]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_api_key(f):
    """Simple API key authentication decorator."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Background compilation worker
# ---------------------------------------------------------------------------
def _compile_worker(job_id: str, video_urls: list, cld_config: dict, folder: str):
    """Run the full download → ffmpeg → upload pipeline in a background thread."""
    job = jobs[job_id]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 1: Download all videos
            downloaded = []
            job["step"] = "downloading"
            for i, url in enumerate(video_urls):
                filepath = os.path.join(tmpdir, f"video_{i:03d}.mp4")
                logger.info(f"[{job_id}] Downloading video {i+1}/{len(video_urls)}")
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
                job["error"] = f"Only {len(downloaded)} videos downloaded successfully, need at least 2"
                return

            # Step 2: Create FFmpeg concat file list
            file_list = os.path.join(tmpdir, "files.txt")
            with open(file_list, "w") as f:
                for fp in downloaded:
                    f.write(f"file '{fp}'\n")

            output_path = os.path.join(tmpdir, "compilation.mp4")

            # Step 3: Try fast concat (stream copy, no re-encoding)
            job["step"] = "compiling"
            job["progress"] = "FFmpeg concat (stream copy)..."
            logger.info(f"[{job_id}] Attempting fast concat (stream copy)...")
            cmd_copy = [
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", file_list,
                "-c", "copy",
                "-movflags", "+faststart",
                "-y", output_path
            ]
            result = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                # Step 3b: Fallback to re-encoding (handles mixed codecs/resolutions)
                job["progress"] = "FFmpeg re-encoding (fallback)..."
                logger.info(f"[{job_id}] Fast concat failed, falling back to re-encoding...")
                cmd_reencode = [
                    "ffmpeg", "-f", "concat", "-safe", "0",
                    "-i", file_list,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                    "-r", "30",
                    "-movflags", "+faststart",
                    "-y", output_path
                ]
                result = subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=1800)
                if result.returncode != 0:
                    logger.error(f"[{job_id}] FFmpeg re-encode failed: {result.stderr[-500:]}")
                    job["status"] = "error"
                    job["error"] = f"FFmpeg compilation failed: {result.stderr[-300:]}"
                    return

            # Check output exists and has size
            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
                job["status"] = "error"
                job["error"] = "Output file is empty or missing"
                return

            output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"[{job_id}] Compilation successful: {output_size_mb:.1f}MB")

            # Step 4: Upload to Cloudinary
            job["step"] = "uploading"
            job["progress"] = f"Uploading {output_size_mb:.0f}MB to Cloudinary..."
            logger.info(f"[{job_id}] Uploading to Cloudinary...")
            cloudinary.config(
                cloud_name=cld_config["cloud_name"],
                api_key=cld_config["api_key"],
                api_secret=cld_config["api_secret"]
            )

            try:
                upload_result = cloudinary.uploader.upload(
                    output_path,
                    resource_type="video",
                    folder=folder,
                    timeout=600
                )
            except Exception as e:
                logger.error(f"[{job_id}] Cloudinary upload failed: {e}")
                job["status"] = "error"
                job["error"] = f"Cloudinary upload failed: {str(e)}"
                return

            logger.info(f"[{job_id}] Upload complete: {upload_result['secure_url']}")

            # Done!
            job["status"] = "done"
            job["step"] = "done"
            job["progress"] = "Complete"
            job["result"] = {
                "url": upload_result["secure_url"],
                "public_id": upload_result["public_id"],
                "duration": upload_result.get("duration", 0),
                "size_mb": round(output_size_mb, 1),
                "videos_compiled": len(downloaded)
            }

    except Exception as e:
        logger.exception(f"[{job_id}] Unexpected error in compile worker")
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    _cleanup_old_jobs()
    active = sum(1 for j in jobs.values() if j["status"] == "processing")
    return jsonify({"status": "ok", "active_jobs": active, "total_jobs": len(jobs)})


@app.route("/compile", methods=["POST"])
@require_api_key
def compile_videos():
    """
    Start an async video compilation job.

    Expected JSON body:
    {
        "urls": ["https://...", "https://...", ...],
        "cloudinary": {
            "cloud_name": "xxx",
            "api_key": "xxx",
            "api_secret": "xxx"
        },
        "folder": "compilations/weekly"  (optional, Cloudinary folder)
    }

    Returns immediately:
    {
        "job_id": "abc123",
        "status": "processing",
        "message": "Compilation started"
    }
    """
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    video_urls = data.get("urls", [])
    cld_config = data.get("cloudinary", {})
    folder = data.get("folder", "compilations")

    if not video_urls:
        return jsonify({"error": "No video URLs provided"}), 400
    if len(video_urls) < 2:
        return jsonify({"error": "Need at least 2 videos to compile"}), 400
    if not all(k in cld_config for k in ("cloud_name", "api_key", "api_secret")):
        return jsonify({"error": "Missing Cloudinary credentials"}), 400

    # Cleanup stale jobs
    _cleanup_old_jobs()

    # Create job
    job_id = uuid.uuid4().hex[:16]
    jobs[job_id] = {
        "status": "processing",
        "step": "queued",
        "progress": "Starting...",
        "created": time.time(),
        "videos": len(video_urls),
        "result": None,
        "error": None
    }

    logger.info(f"[{job_id}] Job created — {len(video_urls)} videos, folder={folder}")

    # Start background thread
    thread = threading.Thread(
        target=_compile_worker,
        args=(job_id, video_urls, cld_config, folder),
        daemon=True
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "processing",
        "videos": len(video_urls),
        "message": "Compilation started"
    }), 202


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def job_status(job_id):
    """
    Check the status of a compilation job.

    Returns:
    - processing: {"job_id": "...", "status": "processing", "step": "...", "progress": "..."}
    - done:       {"job_id": "...", "status": "done", "url": "...", ...}
    - error:      {"job_id": "...", "status": "error", "error": "..."}
    """
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found", "job_id": job_id}), 404

    response = {
        "job_id": job_id,
        "status": job["status"],
        "step": job.get("step", ""),
        "progress": job.get("progress", ""),
        "videos": job.get("videos", 0),
        "elapsed": round(time.time() - job["created"], 1)
    }

    if job["status"] == "done" and job["result"]:
        response.update(job["result"])
    elif job["status"] == "error":
        response["error"] = job["error"]

    return jsonify(response)


# ---------------------------------------------------------------------------
# Legacy sync endpoint (backward-compatible, for testing)
# ---------------------------------------------------------------------------
@app.route("/compile-sync", methods=["POST"])
@require_api_key
def compile_videos_sync():
    """Synchronous compile — kept for backward compatibility / debugging."""
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    video_urls = data.get("urls", [])
    cld_config = data.get("cloudinary", {})
    folder = data.get("folder", "compilations")

    if not video_urls:
        return jsonify({"error": "No video URLs provided"}), 400
    if len(video_urls) < 2:
        return jsonify({"error": "Need at least 2 videos to compile"}), 400
    if not all(k in cld_config for k in ("cloud_name", "api_key", "api_secret")):
        return jsonify({"error": "Missing Cloudinary credentials"}), 400

    job_id = uuid.uuid4().hex[:16]
    jobs[job_id] = {
        "status": "processing",
        "step": "queued",
        "progress": "Starting...",
        "created": time.time(),
        "videos": len(video_urls),
        "result": None,
        "error": None
    }

    _compile_worker(job_id, video_urls, cld_config, folder)

    job = jobs[job_id]
    if job["status"] == "done":
        return jsonify(job["result"])
    else:
        return jsonify({"error": job["error"]}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
