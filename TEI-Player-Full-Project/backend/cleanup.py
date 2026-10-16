import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
media_dir = Path(os.getenv("MEDIA_DIR", "/media")).resolve()
client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=8000)
db = client[os.getenv("MONGODB_DATABASE", "tei_player")]
collection = db["mediaFiles"]
now = datetime.now(timezone.utc)

for item in collection.find({"expires_at": {"$lte": now}}):
    for field in ("file_path", "asset_dir"):
        raw = item.get(field)
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
            path.relative_to(media_dir)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"Cleanup skipped {raw!r}: {exc}")
    collection.delete_one({"_id": item["_id"]})
client.close()
