import threading

from flask import Flask

from app.config import PORT

app = Flask(__name__)


@app.get("/")
def health():
    return {"status": "ok"}


def start_health_server() -> threading.Thread:
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
