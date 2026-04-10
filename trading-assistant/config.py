import os
from dotenv import load_dotenv

load_dotenv()

ETRADE_CONSUMER_KEY = os.environ["ETRADE_CONSUMER_KEY"]
ETRADE_CONSUMER_SECRET = os.environ["ETRADE_CONSUMER_SECRET"]

_env = os.getenv("ETRADE_ENV", "sandbox").lower()
if _env not in ("sandbox", "production"):
    raise ValueError(f"ETRADE_ENV must be 'sandbox' or 'production', got: {_env!r}")

BASE_URL = (
    "https://apisb.etrade.com" if _env == "sandbox" else "https://api.etrade.com"
)
