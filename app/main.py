import logging

from app.bot import build_application
from app.config import TELEGRAM_BOT_TOKEN
from app.db import init_db
from app.health import start_health_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    init_db()
    start_health_server()

    application = build_application()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
