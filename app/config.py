import os

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    ids = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            ids.add(int(chunk))
    return ids


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
ALLOWED_TELEGRAM_USER_IDS = _parse_ids(os.environ.get("ALLOWED_TELEGRAM_USER_IDS", ""))
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///dataroom.db")
PORT = int(os.environ.get("PORT", "8080"))

# Telegram documents over this size can't be delivered via bot API upload.
TELEGRAM_MAX_FILE_BYTES = 50 * 1024 * 1024
