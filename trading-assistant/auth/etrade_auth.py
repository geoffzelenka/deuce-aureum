"""
E*TRADE OAuth1 three-legged flow.

Step 1: Fetch a request token from /oauth/request_token
Step 2: Direct the user to the E*TRADE authorization URL; they paste back a verifier
Step 3: Exchange the request token + verifier for an access token

The resulting tokens are written to SESSION_PATH (./data/session.json) so they
survive process restarts. get_session() loads from disk when in-memory state is
absent. Sessions expire after 2 hours on E*TRADE's side; we enforce a 115-minute
limit to give a 5-minute buffer.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from requests_oauthlib import OAuth1Session

import config

# ---------------------------------------------------------------------------
# Session file
# ---------------------------------------------------------------------------

SESSION_PATH = Path(os.getenv("SESSION_PATH", "./data/session.json"))

SESSION_LIFETIME_SECONDS = 115 * 60  # 115 minutes

# ---------------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------------

_access_token: Optional[str] = None
_access_token_secret: Optional[str] = None
_login_timestamp: Optional[float] = None  # time.monotonic() of login

# Pending OAuth1 request-token state (used by the web login flow)
_pending_resource_key: Optional[str] = None
_pending_resource_secret: Optional[str] = None


# ---------------------------------------------------------------------------
# Disk persistence helpers
# ---------------------------------------------------------------------------

def _save_session() -> None:
    """Write the current access token to SESSION_PATH."""
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps({
            "access_token": _access_token,
            "access_token_secret": _access_token_secret,
            "login_wall_time": time.time(),  # Unix timestamp — survives restarts
        }),
        encoding="utf-8",
    )


def _load_session() -> None:
    """
    Try to load a previously saved session from disk into module-level state.
    Silently does nothing if the file is missing, unreadable, or expired.
    """
    global _access_token, _access_token_secret, _login_timestamp

    if not SESSION_PATH.exists():
        return
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        wall_age = time.time() - data["login_wall_time"]
        if wall_age >= SESSION_LIFETIME_SECONDS:
            SESSION_PATH.unlink(missing_ok=True)
            return
        _access_token = data["access_token"]
        _access_token_secret = data["access_token_secret"]
        # Back-calculate a monotonic timestamp consistent with wall-clock age
        _login_timestamp = time.monotonic() - wall_age
    except Exception:
        pass  # corrupt or unexpected format — ignore, treat as not logged in


def _set_tokens(access_token: str, access_token_secret: str) -> None:
    """Store new tokens in memory and persist to disk."""
    global _access_token, _access_token_secret, _login_timestamp
    _access_token = access_token
    _access_token_secret = access_token_secret
    _login_timestamp = time.monotonic()
    _save_session()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def login() -> None:
    """Run the full three-step OAuth1 flow and persist the resulting credentials."""
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
    _set_tokens(tokens["oauth_token"], tokens["oauth_token_secret"])

    print(f"Login successful. Session is valid for 115 minutes. Saved to {SESSION_PATH}")


def get_session() -> OAuth1Session:
    """
    Return an authenticated OAuth1Session, raising RuntimeError if not logged in
    or the session has expired. Loads from disk on first call if in-memory state
    is absent.
    """
    global _access_token, _login_timestamp

    if _access_token is None:
        _load_session()

    if _access_token is None or _login_timestamp is None:
        raise RuntimeError(
            "Not logged in — please run 'python main.py login' first."
        )

    elapsed = time.monotonic() - _login_timestamp
    if elapsed >= SESSION_LIFETIME_SECONDS:
        SESSION_PATH.unlink(missing_ok=True)
        raise RuntimeError(
            f"Session expired ({elapsed / 60:.1f} min old; limit is 115 min). "
            "Please run 'python main.py login' again."
        )

    return OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        resource_owner_key=_access_token,
        resource_owner_secret=_access_token_secret,
    )


def is_logged_in() -> bool:
    if _access_token is None:
        _load_session()
    if _access_token is None or _login_timestamp is None:
        return False
    return (time.monotonic() - _login_timestamp) < SESSION_LIFETIME_SECONDS


def session_remaining_seconds() -> Optional[int]:
    """Return seconds left in the current session, or None if not logged in."""
    if not is_logged_in():
        return None
    remaining = SESSION_LIFETIME_SECONDS - (time.monotonic() - _login_timestamp)
    return max(0, int(remaining))


def renew_session() -> bool:
    """
    Extend the current session via GET /oauth/renew_access_token.

    Resets the local expiry clock on success. Returns True if E*TRADE
    accepted the renewal, False if it rejected it (e.g. outside market hours
    or the token is already expired on their side). Raises RuntimeError if
    there is no session in memory to renew.
    """
    global _login_timestamp

    if _access_token is None:
        _load_session()
    if _access_token is None:
        raise RuntimeError("Not logged in — no session to renew.")

    session = OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        resource_owner_key=_access_token,
        resource_owner_secret=_access_token_secret,
    )
    try:
        resp = session.get(
            f"{config.BASE_URL}/oauth/renew_access_token",
            timeout=10,
        )
    except Exception:
        return False

    if resp.status_code == 200:
        _login_timestamp = time.monotonic()
        _save_session()
        return True
    return False


def start_login() -> str:
    """
    Begin the OAuth1 three-legged flow (web variant).
    Fetches a request token, stores it in module-level state, and returns the
    authorization URL that the user must open in their browser.
    """
    global _pending_resource_key, _pending_resource_secret

    request_token_url = f"{config.BASE_URL}/oauth/request_token"
    oauth = OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        callback_uri="oob",
    )
    response = oauth.fetch_request_token(request_token_url)
    _pending_resource_key = response["oauth_token"]
    _pending_resource_secret = response["oauth_token_secret"]

    return (
        f"https://us.etrade.com/e/t/etws/authorize"
        f"?key={config.ETRADE_CONSUMER_KEY}&token={_pending_resource_key}"
    )


def complete_login(verifier: str) -> None:
    """
    Complete the OAuth1 flow (web variant) using the stored pending request token
    and a user-supplied verifier code. Persists the resulting access token.
    """
    global _pending_resource_key, _pending_resource_secret

    if _pending_resource_key is None:
        raise RuntimeError("No pending login — call start_login() first.")

    access_token_url = f"{config.BASE_URL}/oauth/access_token"
    oauth = OAuth1Session(
        config.ETRADE_CONSUMER_KEY,
        client_secret=config.ETRADE_CONSUMER_SECRET,
        resource_owner_key=_pending_resource_key,
        resource_owner_secret=_pending_resource_secret,
        verifier=verifier,
    )
    tokens = oauth.fetch_access_token(access_token_url)
    _set_tokens(tokens["oauth_token"], tokens["oauth_token_secret"])

    _pending_resource_key = None
    _pending_resource_secret = None
