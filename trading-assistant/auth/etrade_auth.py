"""
E*TRADE OAuth1 three-legged flow.

Step 1: Fetch a request token from /oauth/request_token
Step 2: Direct the user to the E*TRADE authorization URL; they paste back a verifier
Step 3: Exchange the request token + verifier for an access token

The resulting session is kept in memory only. Sessions expire after 2 hours on
E*TRADE's side; get_session() enforces a 115-minute limit to give a 5-minute buffer.
"""

import time
from typing import Optional

from requests_oauthlib import OAuth1Session

import config

# ---------------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------------

_access_token: Optional[str] = None
_access_token_secret: Optional[str] = None
_login_timestamp: Optional[float] = None

SESSION_LIFETIME_SECONDS = 115 * 60  # 115 minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def login() -> None:
    """Run the full three-step OAuth1 flow and cache the resulting credentials."""
    global _access_token, _access_token_secret, _login_timestamp

    # Step 1 — request token
    request_token_url = f"{config.BASE_URL}/oauth/request_token"
    oauth = OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        callback_uri="oob",  # out-of-band; user will paste the verifier
    )
    response = oauth.fetch_request_token(request_token_url)
    resource_owner_key = response["oauth_token"]
    resource_owner_secret = response["oauth_token_secret"]

    # Step 2 — user authorization
    authorize_url = (
        f"https://us.etrade.com/e/t/etws/authorize"
        f"?key={config.ETRADE_CONSUMER_KEY}&token={resource_owner_key}"
    )
    print(f"\nOpen the following URL in your browser to authorize access:\n\n  {authorize_url}\n")
    verifier = input("Paste the verifier code shown by E*TRADE: ").strip()

    # Step 3 — access token
    access_token_url = f"{config.BASE_URL}/oauth/access_token"
    oauth = OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        resource_owner_key=resource_owner_key,
        resource_owner_secret=resource_owner_secret,
        verifier=verifier,
    )
    tokens = oauth.fetch_access_token(access_token_url)

    _access_token = tokens["oauth_token"]
    _access_token_secret = tokens["oauth_token_secret"]
    _login_timestamp = time.monotonic()

    print("Login successful. Session is valid for 115 minutes.")


def get_session() -> OAuth1Session:
    """Return an authenticated OAuth1Session, raising if the session has expired."""
    if _access_token is None or _login_timestamp is None:
        raise RuntimeError("Not logged in — please run the 'login' command first.")

    elapsed = time.monotonic() - _login_timestamp
    if elapsed >= SESSION_LIFETIME_SECONDS:
        raise RuntimeError(
            f"Session expired — please re-login. "
            f"(Session was {elapsed / 60:.1f} minutes old; limit is 115 minutes.)"
        )

    return OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        resource_owner_key=_access_token,
        resource_owner_secret=_access_token_secret,
    )


def is_logged_in() -> bool:
    if _access_token is None or _login_timestamp is None:
        return False
    return (time.monotonic() - _login_timestamp) < SESSION_LIFETIME_SECONDS
