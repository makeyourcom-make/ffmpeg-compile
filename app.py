"""
FFmpeg Video Compilation Service
Receives video URLs, concatenates them with FFmpeg, uploads to Cloudinary.
Designed for Make.com integration (weekly/monthly video compilations).
"""

import os
import subprocess
import tempfile
import logging
from flask import Flask, request, jsonify
import requests as http_requests
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "change-me-in-production")


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


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/compile", methods=["POST"])
@require_api_key
def compile_videos():
    """
    Compile multiple videos into one using FFmpeg.

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

    Returns:
    {
        "url": "https://res.cloudinary.com/...",
        "public_id": "compilations/weekly/...",
        "duration": 123.45
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

    logger.info(f"Starting compilation of {len(video_urls)} videos")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Download all videos
        downloaded = []
        for i, url in enumerate(video_urls):
            filepath = os.path.join(tmpdir, f"video_{i:03d}.mp4")
            logger.info(f"Downloading video {i+1}/{len(video_urls)}")
            try:
                r = http_requests.get(url, stream=True, timeout=120)
                r.raise_for_status()
                with open(filepath, "wb") as vf:
                    for chunk in r.iter_content(chunk_size=65536):
                        vf.write(chunk)
                downloaded.append(filepath)
            except Exception as e:
                logger.error(f"Failed to download video {i}: {e}")
                continue

        if len(downloaded) < 2:
            return jsonify({"error": f"Only {len(downloaded)} videos downloaded successfully, need at least 2"}), 400

        # Step 2: Create FFmpeg concat file list
        file_list = os.path.join(tmpdir, "files.txt")
        with open(file_list, "w") as f:
            for fp in downloaded:
                f.write(f"file '{fp}'\n")

        output_path = os.path.join(tmpdir, "compilation.mp4")

        # Step 3: Try fast concat (stream copy, no re-encoding)
        logger.info("Attempting fast concat (stream copy)...")
        cmd_copy = [
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", file_list,
            "-c", "copy",
            "-movflags", "+faststart",
            "-y", output_path
        ]
        result = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            # Step 3b: Fallback to re-encoding (handles mixed codecs/resolutions)
            logger.info("Fast concat failed, falling back to re-encoding...")
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
            result = subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                logger.error(f"FFmpeg re-encode failed: {result.stderr[-500:]}")
                return jsonify({"error": "FFmpeg compilation failed", "details": result.stderr[-500:]}), 500

        # Check output exists and has size
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            return jsonify({"error": "Output file is empty or missing"}), 500

        output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"Compilation successful: {output_size_mb:.1f}MB")

        # Step 4: Upload to Cloudinary
        logger.info("Uploading to Cloudinary...")
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
                timeout=300
            )
        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            return jsonify({"error": f"Cloudinary upload failed: {str(e)}"}), 500

        logger.info(f"Upload complete: {upload_result['secure_url']}")

        return jsonify({
            "url": upload_result["secure_url"],
            "public_id": upload_result["public_id"],
            "duration": upload_result.get("duration", 0),
            "size_mb": round(output_size_mb, 1),
            "videos_compiled": len(downloaded)
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
