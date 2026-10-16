import asyncio
import mimetypes
import os
import re
from pathlib import Path
import requests
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_API_URL = os.getenv("BOT_API_URL", "http://api:5055").rstrip("/")
BOT_API_TOKEN = os.environ["BOT_API_TOKEN"]
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/media")).resolve()
PLAYER_BASE_URL = os.getenv("PLAYER_BASE_URL", "").rstrip("/")
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(4 * 1024**3)))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = Client("tei_player_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             workdir="/app/session")

def clean_name(name):
    name = Path(name or "video.bin").name
    name = re.sub(r"[^A-Za-z0-9._() -]", "_", name).strip(" .")
    return (name[:180] or "video.bin")

def register(path, name, mime):
    response = requests.post(
        f"{BOT_API_URL}/api/media/register",
        headers={"X-Bot-Token": BOT_API_TOKEN},
        json={"file_path": str(path), "original_name": name, "mime_type": mime},
        timeout=(10, 3700),
    )
    response.raise_for_status()
    return response.json()

@app.on_message(filters.private & (filters.video | filters.document))
async def on_media(client, message):
    obj = message.video or message.document
    if not obj:
        return
    name = clean_name(getattr(obj, "file_name", None) or f"video_{message.id}.mp4")
    size = int(getattr(obj, "file_size", 0) or 0)
    if size > MAX_DOWNLOAD_BYTES:
        await message.reply_text(
            f"This file exceeds the configured limit of {MAX_DOWNLOAD_BYTES / (1024**3):.1f} GiB. "
            "Telegram and the active Pyrogram/MTProto path may impose a lower limit."
        )
        return
    target = MEDIA_DIR / f"upload_{message.chat.id}_{message.id}_{name}"
    status = await message.reply_text("📥 Downloading original file…")
    try:
        result_path = await client.download_media(message, file_name=str(target))
        if not result_path:
            await status.edit_text("Download failed.")
            return
        path = Path(result_path).resolve()
        try:
            path.relative_to(MEDIA_DIR)
        except ValueError:
            path.unlink(missing_ok=True)
            await status.edit_text("Unsafe file path. Upload cancelled.")
            return
        mime = getattr(obj, "mime_type", None) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        await status.edit_text("🎬 Inspecting media and preparing HLS when compatible…")
        registered = await asyncio.to_thread(register, path, name, mime)
        player_url = registered.get("player_url") or f"{PLAYER_BASE_URL}/watch/{registered['share_id']}"
        if not player_url.startswith(("https://", "http://")):
            raise RuntimeError("PLAYER_BASE_URL is not configured")
        await status.edit_text(
            "✅ **TEI Player link created**\n"
            f"**File:** `{name}`\n"
            f"**HLS:** {'Ready' if registered.get('hls_ready') else 'Unavailable without re-encoding'}\n"
            f"**Expires:** `{registered.get('expires_at', 'configured retention')}`\n"
            f"**Player:** {player_url}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Open TEI Player", url=player_url)]]),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await status.edit_text(
            "❌ Could not create the player link. Check the VPS backend logs and configuration.\n"
            f"`{type(exc).__name__}`"
        )

@app.on_message(filters.private & filters.command("start"))
async def start(_, message):
    await message.reply_text("Send a video or document to create a TEI Player link.")

if __name__ == "__main__":
    print("Starting TEI Player bot…")
    app.run()
