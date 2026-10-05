"""Entry point. Run:  python main.py     Production:  gunicorn main:app"""
from dotenv import load_dotenv
load_dotenv()                      # MUST run before importing app

from app import app, seed          # noqa: E402
import extras                      # noqa: F401,E402  registers promos, reports, etc.

if __name__ == "__main__":
    import os
    with app.app_context():
        seed()
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")