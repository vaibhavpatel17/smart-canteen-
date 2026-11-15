"""Entry point for a production server:   gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT wsgi:app

Reads RAZORPAY_*, KITCHEN_PIN and the rest from the environment (or a .env file), and uses the live canteen:
India time, real payments, no demo data. Keep it at ONE worker process (threads are fine): the login throttle lives in memory
and SQLite is a single file, so more worker processes add risk and no speed for a canteen this size.
"""
import payments
from app import create_app

payments.load_env_file()
app = create_app()
