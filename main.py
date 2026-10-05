"""Entry point. Dev: python main.py.   Production (Render): gunicorn main:app"""
from dotenv import load_dotenv
load_dotenv()

from app import app, seed
import extras

try:
    with app.app_context():
        seed()
except Exception as e:
    print(f"[main] seed skipped: {e}", flush=True)

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
