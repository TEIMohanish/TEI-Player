import asyncio
import html
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pymongo import MongoClient

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_API_URL = os.getenv("BOT_API_URL", "http://api:5055").rstrip("/")
BOT_API_TOKEN = os.environ["BOT_API_TOKEN"]
PLAYER_BASE_URL = os.getenv("PLAYER_BASE_URL", "").rstrip("/")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/media")).resolve()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(4 * 1024**3)))
MAX_CONCURRENT_PER_USER = max(1, int(os.getenv("MAX_CONCURRENT_PER_USER", "1")))
MIN_FREE_DISK_GIB = float(os.getenv("MIN_FREE_DISK_GIB", "2"))
PROGRESS_EDIT_SECONDS = max(2, int(os.getenv("PROGRESS_EDIT_SECONDS", "5")))
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "tei_player")

MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# MongoDB is used for lightweight user records and bot statistics only.
# Media files remain on the VPS disk, never in MongoDB.
mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000) if MONGODB_URI else None
db = mongo[MONGODB_DATABASE] if mongo is not None else None
users_col = db["bot_users"] if db is not None else None
events_col = db["bot_events"] if db is not None else None

app = Client(
    "tei_player_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="/app/session",
    parse_mode=ParseMode.HTML,
)

# In-memory job registry: {user_id: {"task": Task, "status": str, "started": float}}
jobs: dict[int, dict[str, Any]] = {}
job_lock = asyncio.Lock()
bot_started_at = time.time()


def clean_name(name: str | None) -> str:
    name = Path(name or "video.bin").name
    name = re.sub(r"[^A-Za-z0-9._() -]", "_", name).strip(" .")
    return (name[:180] or "video.bin")


def fmt_bytes(value: int | float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "calculating…"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def progress_bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "█" * filled + "░" * (width - filled)


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 How it works", callback_data="help"),
            InlineKeyboardButton("📊 My stats", callback_data="mystats"),
        ],
        [
            InlineKeyboardButton("🛠️ Status", callback_data="status"),
            InlineKeyboardButton("👑 Owner", callback_data="owner_panel"),
        ],
    ])


def owner_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Bot stats", callback_data="owner_stats"),
            InlineKeyboardButton("⚙️ System status", callback_data="owner_system"),
        ],
        [
            InlineKeyboardButton("📣 Broadcast", callback_data="owner_broadcast_help"),
            InlineKeyboardButton("🔄 Refresh", callback_data="owner_panel"),
        ],
        [InlineKeyboardButton("⬅️ Main menu", callback_data="home")],
    ])


def register(path: Path, name: str, mime: str) -> dict:
    response = requests.post(
        f"{BOT_API_URL}/api/media/register",
        headers={"X-Bot-Token": BOT_API_TOKEN},
        json={"file_path": str(path), "original_name": name, "mime_type": mime},
        timeout=(10, 3700),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("share_id"):
        raise RuntimeError("Backend returned an invalid registration response")
    return data


def record_user(user) -> None:
    if users_col is None or user is None:
        return
    try:
        users_col.update_one(
            {"_id": int(user.id)},
            {"$set": {
                "first_name": user.first_name or "",
                "username": user.username or "",
                "last_seen": time.time(),
            }, "$setOnInsert": {"created_at": time.time()}},
            upsert=True,
        )
    except Exception:
        # User tracking must never prevent uploads.
        pass


def record_event(user_id: int, event: str, details: dict | None = None) -> None:
    if events_col is None:
        return
    try:
        events_col.insert_one({
            "user_id": int(user_id),
            "event": event,
            "details": details or {},
            "created_at": time.time(),
        })
    except Exception:
        pass


async def safe_edit(message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def get_free_disk_bytes() -> int:
    return await asyncio.to_thread(lambda: shutil.disk_usage(MEDIA_DIR).free)


async def upload_progress(message, current: int, total: int, state: dict) -> None:
    now = time.monotonic()
    if total <= 0:
        return
    if current < total and now - state.get("last_edit", 0) < PROGRESS_EDIT_SECONDS:
        return
    state["last_edit"] = now
    percent = min(100.0, current * 100 / total)
    elapsed = max(0.1, time.monotonic() - state["started"])
    speed = current / elapsed
    eta = (total - current) / speed if speed > 0 else None
    await safe_edit(
        message,
        "📥 <b>Downloading original file</b>\n\n"
        f"<code>{progress_bar(percent)}</code> <b>{percent:.1f}%</b>\n"
        f"📦 {fmt_bytes(current)} / {fmt_bytes(total)}\n"
        f"⚡ {fmt_bytes(speed)}/s  •  ⏳ {fmt_duration(eta)}\n\n"
        "Use /cancel to stop this upload.",
    )


@app.on_message(filters.private & filters.command("start"))
async def start(_, message):
    record_user(message.from_user)
    first_name = html.escape(message.from_user.first_name or "there")
    await message.reply_text(
        f"👋 <b>Welcome to TEI Player, {first_name}!</b>\n\n"
        "Send me a video or supported media document and I'll create a shareable player link.\n\n"
        "🎞️ Original file is preserved\n"
        "📺 HLS is prepared when compatible\n"
        "⏳ Links expire according to server retention\n\n"
        "Choose an option below or use /help.",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


@app.on_message(filters.private & filters.command("help"))
async def help_command(_, message):
    await message.reply_text(
        "<b>🎬 TEI Player Help</b>\n\n"
        "• Send a video or media document to start processing.\n"
        "• /status — view your active task.\n"
        "• /cancel — cancel your active task.\n"
        "• /mystats — view your account activity.\n"
        "• /help — show this help.\n\n"
        "Large-file support depends on Telegram's actual file-transfer limits, "
        "the client library, VPS disk space and server configuration.",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


@app.on_message(filters.private & filters.command("status"))
async def status_command(_, message):
    user_id = message.from_user.id
    job = jobs.get(user_id)
    if not job:
        await message.reply_text("🟢 No active upload or processing task.")
        return
    await message.reply_text(
        f"🛠️ <b>Your task status:</b> {html.escape(job['status'])}\n"
        f"⏱️ Running for {fmt_duration(time.time() - job['started'])}\n\n"
        "Use /cancel to stop it."
    )


@app.on_message(filters.private & filters.command("cancel"))
async def cancel_command(_, message):
    user_id = message.from_user.id
    job = jobs.get(user_id)
    if not job or not job.get("task") or job["task"].done():
        await message.reply_text("ℹ️ You don't have an active task to cancel.")
        return
    job["task"].cancel()
    await message.reply_text("🛑 Cancellation requested. Cleaning up the partial download…")


@app.on_message(filters.private & filters.command("mystats"))
async def mystats_command(_, message):
    user_id = message.from_user.id
    record_user(message.from_user)
    if events_col is None:
        await message.reply_text(
            "📊 Personal statistics require MongoDB Atlas to be configured for the bot."
        )
        return
    try:
        uploads = events_col.count_documents({"user_id": user_id, "event": "success"})
        failures = events_col.count_documents({"user_id": user_id, "event": "failure"})
        await message.reply_text(
            "📊 <b>Your TEI Player stats</b>\n\n"
            f"✅ Successful links: <b>{uploads}</b>\n"
            f"⚠️ Failed jobs: <b>{failures}</b>"
        )
    except Exception:
        await message.reply_text("⚠️ Stats are temporarily unavailable.")


@app.on_message(filters.private & filters.command("admin"))
async def admin_command(_, message):
    if not is_owner(message.from_user.id):
        await message.reply_text("⛔ This command is owner-only.")
        return
    await message.reply_text(
        "👑 <b>TEI Player Owner Console</b>\n\nChoose an admin action:",
        reply_markup=owner_keyboard(),
    )


@app.on_message(filters.private & filters.command("broadcast"))
async def broadcast_command(_, message):
    if not is_owner(message.from_user.id):
        await message.reply_text("⛔ This command is owner-only.")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.reply_text(
            "Usage:\n<code>/broadcast Your announcement here</code>\n\n"
            "This sends a text announcement to users recorded in MongoDB."
        )
        return
    if users_col is None:
        await message.reply_text("⚠️ Configure MONGODB_URI before using broadcast.")
        return
    await message.reply_text(
        "📣 Broadcast started. I'll report the final delivery counts when complete."
    )
    sent = failed = 0
    try:
        user_ids = await asyncio.to_thread(
            lambda: [int(doc["_id"]) for doc in users_col.find({}, {"_id": 1})]
        )
    except Exception:
        await message.reply_text("❌ Could not read users from MongoDB.")
        return
    for uid in user_ids:
        try:
            await app.send_message(uid, f"📣 <b>TEI Player announcement</b>\n\n{html.escape(text)}")
            sent += 1
        except FloodWait as exc:
            await asyncio.sleep(exc.value + 1)
            try:
                await app.send_message(uid, f"📣 <b>TEI Player announcement</b>\n\n{html.escape(text)}")
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.reply_text(
        f"📣 <b>Broadcast finished</b>\n\n✅ Sent: {sent}\n⚠️ Failed: {failed}"
    )


@app.on_message(filters.private & (filters.video | filters.document))
async def on_media(client, message):
    user = message.from_user
    if user is None:
        return
    record_user(user)

    # Avoid overlapping jobs for one person.
    async with job_lock:
        current = jobs.get(user.id)
        if current and current.get("task") and not current["task"].done():
            await message.reply_text(
                "⏳ You already have an active task. Wait for it to finish or use /cancel."
            )
            return
        task = asyncio.current_task()
        jobs[user.id] = {"task": task, "status": "Starting", "started": time.time()}

    obj = message.video or message.document
    name = clean_name(getattr(obj, "file_name", None) or f"video_{message.id}.mp4")
    size = int(getattr(obj, "file_size", 0) or 0)
    status_msg = None
    target = MEDIA_DIR / f"upload_{user.id}_{message.id}_{name}"
    registered_ok = False

    try:
        if size <= 0:
            await message.reply_text("❌ Telegram did not provide a valid file size.")
            return
        if size > MAX_DOWNLOAD_BYTES:
            await message.reply_text(
                f"❌ File is {fmt_bytes(size)}, above the configured limit of "
                f"{fmt_bytes(MAX_DOWNLOAD_BYTES)}.\n\n"
                "This setting cannot override Telegram/Pyrogram transfer limits."
            )
            return
        if not name.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts", ".m3u8", ".mp3", ".m4a")):
            if message.video is None:
                await message.reply_text(
                    "⚠️ This document doesn't have a common media extension. "
                    "I'll still try to inspect it, but playback may not be supported."
                )
        free_bytes = await get_free_disk_bytes()
        if free_bytes < size + int(MIN_FREE_DISK_GIB * 1024**3):
            await message.reply_text(
                f"❌ Not enough free VPS disk space.\nFree: {fmt_bytes(free_bytes)}\n"
                f"Required file space plus safety margin: about "
                f"{fmt_bytes(size + int(MIN_FREE_DISK_GIB * 1024**3))}."
            )
            return

        status_msg = await message.reply_text(
            "📥 <b>Preparing download…</b>\n\n"
            f"📄 <code>{html.escape(name)}</code>\n"
            f"📦 {fmt_bytes(size)}\n"
            "Use /cancel to stop.",
        )
        jobs[user.id]["status"] = "Downloading"
        progress_state = {"started": time.monotonic(), "last_edit": 0.0}

        async def progress(current: int, total: int, *_):
            await upload_progress(status_msg, current, total, progress_state)

        result_path = await client.download_media(
            message,
            file_name=str(target),
            progress=progress,
        )
        if not result_path:
            raise RuntimeError("Telegram returned no downloaded file path")
        path = Path(result_path).resolve()
        try:
            path.relative_to(MEDIA_DIR)
        except ValueError:
            path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded file resolved outside the media directory")

        actual_size = path.stat().st_size
        if actual_size <= 0:
            raise RuntimeError("Downloaded file is empty")

        jobs[user.id]["status"] = "Registering media with API"
        await safe_edit(
            status_msg,
            "🔎 <b>Download complete</b>\n\n"
            f"📦 {fmt_bytes(actual_size)}\n"
            "🎬 Inspecting media and preparing HLS when compatible…",
        )
        mime = getattr(obj, "mime_type", None) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        registered = await asyncio.to_thread(register, path, name, mime)
        player_url = registered.get("player_url")
        if not player_url:
            if not PLAYER_BASE_URL:
                raise RuntimeError("PLAYER_BASE_URL is not configured")
            player_url = f"{PLAYER_BASE_URL}/watch/{registered['share_id']}"
        if not player_url.startswith("https://"):
            raise RuntimeError("Player URL must use HTTPS in production")

        registered_ok = True
        record_event(user.id, "success", {
            "share_id": str(registered.get("share_id", "")),
            "file_size": actual_size,
            "mime_type": mime,
        })
        hls_ready = bool(registered.get("hls_ready"))
        expires = html.escape(str(registered.get("expires_at", "according to retention settings")))
        safe_player_url = html.escape(player_url, quote=True)
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Open TEI Player", url=player_url)],
            [
                InlineKeyboardButton("🔗 Copy/share link", url=player_url),
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
        ])
        await safe_edit(
            status_msg,
            "✅ <b>TEI Player link created!</b>\n\n"
            f"📄 <b>File:</b> <code>{html.escape(name)}</code>\n"
            f"📦 <b>Size:</b> {fmt_bytes(actual_size)}\n"
            f"📺 <b>HLS:</b> {'🟢 Ready' if hls_ready else '🟠 Not available without compatible stream-copy packaging'}\n"
            f"⏳ <b>Expires:</b> <code>{expires}</code>\n\n"
            f"🔗 <a href=\"{safe_player_url}\">Open your player</a>",
            reply_markup=buttons,
        )
    except asyncio.CancelledError:
        if status_msg:
            await safe_edit(status_msg, "🛑 <b>Task cancelled.</b> Cleaning up the partial file.")
        raise
    except Exception as exc:
        record_event(user.id, "failure", {"error_type": type(exc).__name__})
        if status_msg:
            await safe_edit(
                status_msg,
                "❌ <b>Could not create the player link.</b>\n\n"
                f"Error type: <code>{html.escape(type(exc).__name__)}</code>\n"
                "Check backend logs and environment settings.",
            )
        else:
            await message.reply_text(
                f"❌ Could not start processing: <code>{html.escape(type(exc).__name__)}</code>"
            )
    finally:
        if not registered_ok:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
        async with job_lock:
            current = jobs.get(user.id)
            if current and current.get("task") is asyncio.current_task():
                jobs.pop(user.id, None)


@app.on_callback_query()
async def callback_handler(_, query):
    user = query.from_user
    data = query.data or ""
    if data == "home":
        await query.answer()
        await query.message.edit_text(
            "🎬 <b>TEI Player</b>\n\nSend a video or media document to create a shareable link.",
            reply_markup=main_keyboard(),
        )
        return
    if data == "help":
        await query.answer()
        await query.message.edit_text(
            "<b>🎬 TEI Player Help</b>\n\n"
            "Send a video or media document to create a player link.\n"
            "/status — active task\n/cancel — cancel task\n/mystats — your stats\n"
            "Large-file support depends on Telegram's actual transfer limits and VPS resources.",
            reply_markup=main_keyboard(),
        )
        return
    if data == "status":
        await query.answer()
        job = jobs.get(user.id)
        if not job:
            text = "🟢 No active task."
        else:
            text = (
                f"🛠️ <b>Status:</b> {html.escape(job['status'])}\n"
                f"⏱️ Running for {fmt_duration(time.time() - job['started'])}"
            )
        await query.message.reply_text(text)
        return
    if data == "mystats":
        await query.answer()
        if events_col is None:
            await query.message.reply_text("📊 Configure MongoDB Atlas to enable personal stats.")
            return
        try:
            uploads = events_col.count_documents({"user_id": user.id, "event": "success"})
            failures = events_col.count_documents({"user_id": user.id, "event": "failure"})
            await query.message.reply_text(
                f"📊 <b>Your stats</b>\n\n✅ Links: {uploads}\n⚠️ Failed jobs: {failures}"
            )
        except Exception:
            await query.message.reply_text("⚠️ Stats temporarily unavailable.")
        return
    if data.startswith("owner_"):
        if not is_owner(user.id):
            await query.answer("Owner only.", show_alert=True)
            return
        await query.answer()
        if data == "owner_panel":
            await query.message.edit_text(
                "👑 <b>TEI Player Owner Console</b>\n\nChoose an admin action:",
                reply_markup=owner_keyboard(),
            )
        elif data == "owner_stats":
            try:
                total_users = users_col.count_documents({}) if users_col is not None else "N/A"
                total_success = events_col.count_documents({"event": "success"}) if events_col is not None else "N/A"
                total_fail = events_col.count_documents({"event": "failure"}) if events_col is not None else "N/A"
                active = len([j for j in jobs.values() if j.get("task") and not j["task"].done()])
                await query.message.edit_text(
                    "📊 <b>TEI Player Bot Statistics</b>\n\n"
                    f"👥 Users: <b>{total_users}</b>\n"
                    f"✅ Successful links: <b>{total_success}</b>\n"
                    f"⚠️ Failed jobs: <b>{total_fail}</b>\n"
                    f"🔄 Active jobs: <b>{active}</b>\n"
                    f"⏱️ Uptime: <b>{fmt_duration(time.time() - bot_started_at)}</b>",
                    reply_markup=owner_keyboard(),
                )
            except Exception:
                await query.message.edit_text("⚠️ Could not load statistics.", reply_markup=owner_keyboard())
        elif data == "owner_system":
            try:
                free = await get_free_disk_bytes()
                total = shutil.disk_usage(MEDIA_DIR).total
                mongo_state = "🟢 Configured" if mongo is not None else "🔴 Missing MONGODB_URI"
                await query.message.edit_text(
                    "⚙️ <b>System status</b>\n\n"
                    f"💽 Free disk: <b>{fmt_bytes(free)}</b> / {fmt_bytes(total)}\n"
                    f"🗄️ MongoDB: <b>{mongo_state}</b>\n"
                    f"📁 Media directory: <code>{html.escape(str(MEDIA_DIR))}</code>\n"
                    f"📦 Configured limit: <b>{fmt_bytes(MAX_DOWNLOAD_BYTES)}</b>\n"
                    "ℹ️ This configured limit does not override Telegram transfer limits.",
                    reply_markup=owner_keyboard(),
                )
            except Exception:
                await query.message.edit_text("⚠️ Could not read system status.", reply_markup=owner_keyboard())
        elif data == "owner_broadcast_help":
            await query.message.edit_text(
                "📣 <b>Broadcast instructions</b>\n\n"
                "Send a new message using:\n<code>/broadcast Your announcement</code>\n\n"
                "Only users who have started the bot and are recorded in MongoDB are included.",
                reply_markup=owner_keyboard(),
            )
        return
    await query.answer()


async def main():
    print("Starting TEI Player bot…")
    if mongo is not None:
        try:
            await asyncio.to_thread(mongo.admin.command, "ping")
            print(f"MongoDB connected; database={MONGODB_DATABASE}")
        except Exception as exc:
            print(f"WARNING: MongoDB unavailable ({type(exc).__name__}); uploads can still work, stats will be limited.")
    await app.start()
    print("TEI Player bot started.")
    await idle()
    await app.stop()
    if mongo is not None:
        mongo.close()


if __name__ == "__main__":
    app.run(main())
