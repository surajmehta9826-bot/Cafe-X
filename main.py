"""Entry point. Dev: python main.py.   Production (Render): gunicorn main:app"""
from dotenv import load_dotenv
load_dotenv()

from app import app, seed           # noqa: E402
import extras                       # noqa: F401,E402

try:
    with app.app_context():
        seed()
except Exception as e:
    print(f"[main] seed skipped: {e}", flush=True)


# ============================================================
#  KEEP-ALIVE — self-ping every 10 min to fight Render sleep
# ============================================================
import os, threading, time, urllib.request

def keep_alive():
    base = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
    url = base + "/healthz"
    print(f"[keepalive] pinging {url} every 10 min", flush=True)
    while True:
        time.sleep(600)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CafeX-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"[keepalive] OK {r.status}", flush=True)
        except Exception as e:
            print(f"[keepalive] fail: {e}", flush=True)

if os.getenv("RENDER") and not os.getenv("KEEPALIVE_DISABLED"):
    threading.Thread(target=keep_alive, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")