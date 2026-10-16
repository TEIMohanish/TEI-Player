import hmac
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, Response
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_BYTES", str(4 * 1024**3)))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/media")).resolve()
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
RETENTION_HOURS = max(1, int(os.getenv("FILE_RETENTION_HOURS", "6")))
PLAYER_BASE_URL = os.getenv("PLAYER_BASE_URL", "").rstrip("/")
PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "").rstrip("/")
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "")
origins = [v.strip() for v in os.getenv("CORS_ORIGINS", "").split(",") if v.strip()]
CORS(app, resources={r"/api/*": {"origins": origins}})

mongo_uri = os.getenv("MONGODB_URI", "")
if not mongo_uri:
    raise RuntimeError("MONGODB_URI is required")
client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
db = client[os.getenv("MONGODB_DATABASE", "tei_player")]
media = db["mediaFiles"]
media.create_index([("share_id", ASCENDING)], unique=True)
media.create_index([("expires_at", ASCENDING)])

def utcnow():
    return datetime.now(timezone.utc)

def managed_path(raw):
    path = Path(raw).resolve()
    try:
        path.relative_to(MEDIA_DIR)
    except ValueError:
        abort(400, description="Path is outside managed media directory")
    return path

def require_bot():
    token = request.headers.get("X-Bot-Token", "")
    if not BOT_API_TOKEN or not hmac.compare_digest(token, BOT_API_TOKEN):
        abort(401)

def probe_file(path):
    try:
        raw = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,channels,channel_layout,sample_rate",
            "-of", "json", str(path)
        ], stderr=subprocess.STDOUT, timeout=30)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}

def make_hls(source, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    playlist = output_dir / "source.m3u8"
    segment_pattern = output_dir / "segment_%06d.ts"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
        "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(segment_pattern), str(playlist)
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600, capture_output=True)
        if playlist.is_file() and playlist.stat().st_size:
            return True
    except Exception:
        pass
    shutil.rmtree(output_dir, ignore_errors=True)
    return False

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        db_state = "ok"
    except Exception:
        db_state = "error"
    return jsonify({"status": "ok" if db_state == "ok" else "degraded",
                    "database": db_state, "ffmpeg": shutil.which("ffmpeg") is not None})

@app.post("/api/media/register")
def register():
    require_bot()
    body = request.get_json(silent=True) or {}
    raw_path = body.get("file_path")
    if not raw_path:
        return jsonify({"error": "file_path is required"}), 400
    path = managed_path(raw_path)
    if not path.is_file():
        return jsonify({"error": "file not found"}), 404
    size = path.stat().st_size
    if size <= 0 or size > app.config["MAX_CONTENT_LENGTH"]:
        return jsonify({"error": "file size invalid or exceeds limit"}), 413

    original_name = Path(str(body.get("original_name") or path.name)).name
    mime = str(body.get("mime_type") or mimetypes.guess_type(original_name)[0] or "application/octet-stream")
    share_id = secrets.token_urlsafe(18)
    asset_dir = MEDIA_DIR / f"{share_id}_hls"
    details = probe_file(path)
    hls_ready = make_hls(path, asset_dir)
    now = utcnow()
    expires = now + timedelta(hours=RETENTION_HOURS)
    doc = {
        "share_id": share_id, "original_name": original_name, "mime_type": mime,
        "file_path": str(path), "size": size, "probe": details,
        "hls_ready": hls_ready, "asset_dir": str(asset_dir) if hls_ready else None,
        "created_at": now, "expires_at": expires
    }
    media.insert_one(doc)
    player_url = f"{PLAYER_BASE_URL}/watch/{quote(share_id)}" if PLAYER_BASE_URL else None
    return jsonify({"share_id": share_id, "player_url": player_url,
                    "expires_at": expires.isoformat(), "hls_ready": hls_ready}), 201

def active_doc(share_id):
    item = media.find_one({"share_id": share_id})
    if not item:
        abort(404)
    if item.get("expires_at") and item["expires_at"] <= utcnow():
        abort(410, description="This link has expired")
    return item

@app.get("/api/media/<share_id>")
def get_info(share_id):
    item = active_doc(share_id)
    probe = item.get("probe") or {}
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    fmt = probe.get("format") or {}
    return jsonify({
        "id": item["share_id"], "name": item["original_name"], "size": item["size"],
        "mime_type": item["mime_type"], "created_at": item["created_at"].isoformat(),
        "expires_at": item["expires_at"].isoformat(), "hls_ready": bool(item.get("hls_ready")),
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "width": video.get("width"), "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_tracks": [{"index": s.get("index"), "codec": s.get("codec_name"),
                          "channels": s.get("channels")} for s in audio],
        "subtitle_tracks": [{"index": s.get("index"), "codec": s.get("codec_name")} for s in subs],
        "hls_url": f"{PUBLIC_API_BASE_URL}/media/{quote(share_id)}/master.m3u8" if item.get("hls_ready") else None,
        "download_url": f"{PUBLIC_API_BASE_URL}/media/{quote(share_id)}/download"
    })

@app.get("/api/media/<share_id>/master.m3u8")
def master_playlist(share_id):
    item = active_doc(share_id)
    if not item.get("hls_ready"):
        abort(404, description="HLS unavailable for this source")
    playlist = managed_path(str(Path(item["asset_dir"]) / "source.m3u8"))
    if not playlist.is_file():
        abort(404)
    text = playlist.read_text(encoding="utf-8", errors="replace")
    prefix = f"{PUBLIC_API_BASE_URL}/media/{quote(share_id)}/hls/"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(prefix + quote(Path(stripped).name))
        else:
            lines.append(line)
    response = Response("\n".join(lines) + "\n", mimetype="application/vnd.apple.mpegurl")
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/api/media/<share_id>/hls/<path:filename>")
def hls_file(share_id, filename):
    item = active_doc(share_id)
    if not item.get("hls_ready"):
        abort(404)
    asset_dir = managed_path(item["asset_dir"])
    path = managed_path(str(asset_dir / filename))
    try:
        path.relative_to(asset_dir)
    except ValueError:
        abort(400)
    if not path.is_file():
        abort(404)
    mime = "video/mp2t" if path.suffix.lower() == ".ts" else "application/vnd.apple.mpegurl"
    response = send_file(path, mimetype=mime, conditional=True)
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/api/media/<share_id>/download")
def download(share_id):
    item = active_doc(share_id)
    path = managed_path(item["file_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=Path(item["original_name"]).name,
                     mimetype=item.get("mime_type") or "application/octet-stream", conditional=True)

@app.errorhandler(401)
def err401(_): return jsonify({"error": "Unauthorized"}), 401
@app.errorhandler(404)
def err404(_): return jsonify({"error": "Not found"}), 404
@app.errorhandler(410)
def err410(e): return jsonify({"error": getattr(e, "description", "Expired")}), 410
@app.errorhandler(413)
def err413(_): return jsonify({"error": "Request too large"}), 413
