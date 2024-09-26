import os
import sys

RAW_API_ID = os.environ.get("API_ID") or sys.exit("API_ID environment variable not set")
API_ID = int(RAW_API_ID)
API_HASH = os.environ.get("API_HASH") or sys.exit("API_HASH environment variable not set")
ADMIN_ID = os.environ.get("ADMIN_ID") or sys.exit("ADMIN_ID environment variable not set")
TRACKING_CHAT_IDS = list(map(int, filter(lambda x: len(x) > 0, os.environ.get("TRACKING_CHAT_IDS").split(","))))
if len(TRACKING_CHAT_IDS) == 0:
    sys.exit("TRACKING_CHAT_IDS")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY environment variable not set")
DB_PATH = os.environ.get("DB_PATH") or sys.exit("DB_PATH environment variable not set")
SESSION_PATH = os.environ.get("SESSION_PATH") or sys.exit("SESSION_PATH environment variable not set")
