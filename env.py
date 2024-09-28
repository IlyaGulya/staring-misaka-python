import os
import sys
import typing


def require_env(key: str) -> typing.Optional[str]:
    return os.environ.get(key) or sys.exit(f"{key} environment variable is not set")


RAW_API_ID = require_env("API_ID")
API_ID = int(RAW_API_ID)
API_HASH = require_env("API_HASH")
RAW_ADMIN_ID = require_env("ADMIN_ID")
ADMIN_ID = int(RAW_ADMIN_ID)
TRACKING_CHAT_IDS = (
    list(
        map(
            int,
            filter(
                lambda x: len(x) > 0,
                require_env("TRACKING_CHAT_IDS").split(",")
            )
        )
    )
)
if len(TRACKING_CHAT_IDS) == 0:
    sys.exit("TRACKING_CHAT_IDS environment variable not set")
ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY")
DB_PATH = require_env("DB_PATH")
SESSION_PATH = require_env("SESSION_PATH")
