"""
FFmpeg Video Compilation Service
Receives video URLs, concatenates them with FFmpeg, uploads to Cloudflare R2
via the media-onedrive-proxy Worker.

POST /compile          → returns job_id immediately (async)
GET  /status/<job_id>  → progress + R2 URL when done
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

# Azure OneDrive (Microsoft Graph) — for chunked backup upload of compilations.
# Avoids n8n OOM by streaming directly from disk to OneDrive in 4 MB chunks.
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
AZURE_REFRESH_TOKEN = os.environ.get("AZURE_REFRESH_TOKEN", "")
_token_cache = {"access_token": None, "expires_at": 0, "refresh_token": AZURE_REFRESH_TOKEN}

jobs: dict = {}
JOBS_MAX_AGE = 7200  # auto-clean jobs older than 2 hours


def _get_onedrive_token():
    """Returns a fresh access token using the stored refresh token. Cached in-memory."""
    if _token_cache["access_token"] and _token_cache["expires_at"] > time.time() + 60:
        return _token_cache["access_token"]
    if not (AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and _token_cache["refresh_token"]):
        raise RuntimeError("Azure credentials not configured (CLIENT_ID/SECRET/REFRESH_TOKEN)")
    r = http_requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": AZURE_CLIENT_ID,
            "client_secret": AZURE_CLIENT_SECRET,
            "refresh_token": _token_cache["refresh_token"],
            "grant_type": "refresh_token",
            "scope": "Files.ReadWrite.All offline_access",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    # Microsoft rotates refresh tokens — store the new one in-process for this run
    if "refresh_token" in data:
        _token_cache["refresh_token"] = data["refresh_token"]
    return _token_cache["access_token"]


def _upload_to_onedrive(file_path: str, parent_id: str, filename: str):
    """Chunked upload via Graph upload session. Returns (item_id, web_url, size_bytes)."""
    token = _get_onedrive_token()
    create_url = (
        f"https://graph.microsoft.com/v1.0/me/drive/items/{parent_id}:/{filename}:"
        f"/createUploadSession"
    )
    r = http_requests.post(
        create_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": filename}},
        timeout=30,
    )
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]

    # 4 MB chunks (must be a multiple of 320 KiB per Graph API spec)
    CHUNK_SIZE = 4 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    item = None
    with open(file_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            chunk_end = offset + len(chunk) - 1
            up = http_requests.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{chunk_end}/{file_size}",
                },
                data=chunk,
                timeout=300,
            )
            if up.status_code in (200, 201):
                item = up.json()
                break
            elif up.status_code == 202:
                offset = chunk_end + 1
            else:
                raise RuntimeError(
                    f"Chunked upload failed at {offset}: {up.status_code} {up.text[:300]}"
                )
    if not item:
        raise RuntimeError("Upload finished without final response")
    return item["id"], item.get("webUrl", ""), item.get("size", file_size)


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

            # 2. Normalize each clip individually (low RAM usage — 1 file at a time)
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
                    "scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
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
            job["progress"] = f"Concat {len(normalized)} normalized clips (full)..."
            cmd_concat = [
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", file_list,
                "-c", "copy",
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

            # 3b. Optional TikTok-capped variant (stream-copy trim — runs in seconds)
            tiktok_path = None
            tiktok_key = job.get("tiktok_key")
            if tiktok_key:
                tiktok_path = os.path.join(tmpdir, "compilation_tiktok.mp4")
                job["progress"] = "Trimming TikTok variant to 570s..."
                cmd_trim = [
                    "ffmpeg", "-i", output_path,
                    "-c", "copy",
                    "-t", "570",
                    "-movflags", "+faststart",
                    "-y", tiktok_path,
                ]
                trim = subprocess.run(cmd_trim, capture_output=True, text=True, timeout=120)
                if trim.returncode != 0 or not os.path.exists(tiktok_path):
                    logger.error(f"[{job_id}] TikTok trim failed: {trim.stderr[-300:]}")
                    tiktok_path = None  # non-fatal — TikTok URL just won't be returned

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

            result = {
                "url": upload_response["url"],
                "key": upload_response["key"],
                "size_mb": round(output_size_mb, 1),
                "videos_compiled": len(downloaded),
            }

            # 4b. Upload TikTok-capped variant to R2 (separate key)
            if tiktok_path:
                job["progress"] = f"Uploading TikTok variant to R2..."
                try:
                    with open(tiktok_path, "rb") as f:
                        rt = http_requests.post(
                            f"{WORKER_UPLOAD_URL}?key={tiktok_key}",
                            headers={
                                "Authorization": f"Bearer {WORKER_UPLOAD_SECRET}",
                                "Content-Type": "video/mp4",
                            },
                            data=f,
                            timeout=900,
                        )
                    rt.raise_for_status()
                    tiktok_response = rt.json()
                    tiktok_size_mb = os.path.getsize(tiktok_path) / (1024 * 1024)
                    result["tiktok_url"] = tiktok_response["url"]
                    result["tiktok_key"] = tiktok_response["key"]
                    result["tiktok_size_mb"] = round(tiktok_size_mb, 1)
                    logger.info(f"[{job_id}] TikTok variant uploaded: {tiktok_response['url']} ({tiktok_size_mb:.1f}MB)")
                except Exception as e:
                    logger.error(f"[{job_id}] TikTok variant upload failed (non-fatal): {e}")
                    result["tiktok_error"] = str(e)

            # 5. Optional: chunked backup to OneDrive (skipped silently if not requested)
            onedrive_filename = job.get("onedrive_filename")
            onedrive_parent_id = job.get("onedrive_parent_id")
            if onedrive_filename and onedrive_parent_id:
                job["step"] = "uploading_onedrive"
                job["progress"] = f"Backup {output_size_mb:.0f}MB to OneDrive..."
                try:
                    item_id, web_url, _sz = _upload_to_onedrive(
                        output_path, onedrive_parent_id, onedrive_filename
                    )
                    result["onedrive_id"] = item_id
                    result["onedrive_url"] = web_url
                    logger.info(f"[{job_id}] OneDrive backup OK: {item_id}")
                except Exception as od_err:
                    logger.error(f"[{job_id}] OneDrive backup failed (non-fatal): {od_err}")
                    result["onedrive_error"] = str(od_err)

            job["status"] = "done"
            job["step"] = "done"
            job["progress"] = "Complete"
            job["result"] = result

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
        "onedrive_configured": bool(
            AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and _token_cache["refresh_token"]
        ),
    })


@app.route("/oauth/start", methods=["GET"])
def oauth_start():
    """One-time OAuth flow to obtain a refresh_token for OneDrive uploads.
    Visit this URL in a browser while signed in to your Microsoft account.
    """
    if not AZURE_CLIENT_ID:
        return "AZURE_CLIENT_ID env var not set", 500
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    auth_url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        f"?client_id={AZURE_CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={redirect_uri}"
        "&response_mode=query"
        "&scope=Files.ReadWrite.All%20offline_access%20User.Read"
        "&prompt=consent"
    )
    return f'<html><body><h1>OAuth Setup</h1><p>Redirect URI registered: <code>{redirect_uri}</code></p><p><a href="{auth_url}">→ Sign in with Microsoft to grant access</a></p></body></html>'


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Exchanges the auth code for a refresh_token, then displays it for env-var setup."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<pre>OAuth error: {error}\n{request.args.get('error_description', '')}</pre>", 400
    if not code:
        return "Missing code parameter", 400
    if not (AZURE_CLIENT_ID and AZURE_CLIENT_SECRET):
        return "AZURE_CLIENT_ID/SECRET not configured on server", 500
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    try:
        r = http_requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id": AZURE_CLIENT_ID,
                "client_secret": AZURE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "scope": "Files.ReadWrite.All offline_access User.Read",
            },
            timeout=30,
        )
        r.raise_for_status()
        tokens = r.json()
    except Exception as e:
        return f"<pre>Token exchange failed: {e}\n{getattr(e, 'response', None) and e.response.text[:500]}</pre>", 500
    refresh_token = tokens.get("refresh_token", "")
    # Cache in-memory for immediate testing without a redeploy
    _token_cache["refresh_token"] = refresh_token
    _token_cache["access_token"] = tokens.get("access_token")
    _token_cache["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return (
        "<html><body><h1>✓ OAuth success</h1>"
        "<p><b>Copy this refresh_token</b> into the Render env var <code>AZURE_REFRESH_TOKEN</code>:</p>"
        f"<textarea rows=8 cols=120 onclick='this.select()'>{refresh_token}</textarea>"
        "<p>The token has also been cached in memory — you can immediately test "
        '<a href="/test-onedrive">/test-onedrive</a> (with X-API-Key header).</p>'
        "<p>After saving to Render env vars, the service will restart and re-load the token "
        "permanently.</p></body></html>"
    )


@app.route("/move-onedrive", methods=["POST"])
@require_api_key
def move_onedrive():
    """Move a OneDrive item to a new parent folder via Microsoft Graph PATCH.

    The n8n Microsoft OneDrive node's "move" operation silently no-ops on personal
    accounts, so daily workflows call this endpoint instead.

    Body: {"file_id": "AB3777D49B68228!s...", "parent_id": "AB3777D49B68228!s..."}
    """
    data = request.json or {}
    file_id = data.get("file_id")
    parent_id = data.get("parent_id")
    if not (file_id and parent_id):
        return jsonify({"ok": False, "error": "file_id and parent_id required"}), 400
    try:
        token = _get_onedrive_token()
        r = http_requests.patch(
            f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"parentReference": {"id": parent_id}},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        return jsonify({
            "ok": True,
            "id": info.get("id"),
            "name": info.get("name"),
            "new_parent": info.get("parentReference", {}).get("id"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/list-folders", methods=["GET"])
@require_api_key
def list_folders():
    """List children of an OneDrive folder. Pass ?parent=<id> or omit for root.
    Used during multi-brand replication to discover folder IDs.
    """
    parent = request.args.get("parent")
    try:
        token = _get_onedrive_token()
        if parent:
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{parent}/children"
        else:
            url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        r = http_requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id,name,folder,parentReference", "$top": 200},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("value", [])
        return jsonify({"parent": parent or "root", "count": len(items), "items": [
            {"id": i["id"], "name": i["name"], "is_folder": "folder" in i}
            for i in items
        ]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/test-onedrive", methods=["GET"])
@require_api_key
def test_onedrive():
    """Verify Azure refresh_token works by minting an access token and listing root."""
    try:
        token = _get_onedrive_token()
        r = http_requests.get(
            "https://graph.microsoft.com/v1.0/me/drive/root",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        r.raise_for_status()
        info = r.json()
        return jsonify({
            "ok": True,
            "drive_id": info.get("parentReference", {}).get("driveId"),
            "drive_name": info.get("name"),
            "owner": info.get("createdBy", {}).get("user", {}).get("displayName"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    tiktok_key = data.get("tiktok_key")  # optional — produces a 570s trimmed variant
    onedrive_filename = data.get("onedrive_filename")  # optional
    onedrive_parent_id = data.get("onedrive_parent_id")  # optional

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
        "tiktok_key": tiktok_key,
        "onedrive_filename": onedrive_filename,
        "onedrive_parent_id": onedrive_parent_id,
    }

    logger.info(f"[{job_id}] Job created — {len(video_urls)} videos → {r2_key}")

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
