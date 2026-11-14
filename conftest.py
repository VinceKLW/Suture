"""Pytest-wide fixtures: load .env and certifi CA bundle before any import.

Without this, pymongo on Python 3.13/macOS fails SSL verification against Atlas,
and tests that skip on `MONGODB_URI` see an empty env because .env was never read.
"""
import os

import certifi
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
