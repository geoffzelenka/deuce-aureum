Create a Python project called `trading-assistant` with the following structure:

trading-assistant/
  main.py              # CLI entry point
  config.py            # loads .env (ETRADE_CONSUMER_KEY, ETRADE_CONSUMER_SECRET)
  auth/
    etrade_auth.py     # OAuth1 login flow
  store/
    db.py              # SQLite setup and headline storage
  report/
    generator.py       # morning report logic (stub for now)
  monitor/
    watcher.py         # price polling loop (stub for now)
  alerts/
    notifier.py        # alert dispatch (stub for now)
  web/
    app.py             # Flask web UI (stub for now)
  requirements.txt
  .env.example

Requirements:
- Use `requests-oauthlib` for OAuth1.
- E*TRADE's sandbox base URL is https://apisb.etrade.com; production is https://api.etrade.com. Make this configurable via .env (ETRADE_ENV=sandbox|production).
- The OAuth1 flow for E*TRADE requires three steps: (1) get a request token at /oauth/request_token, (2) redirect the user to https://us.etrade.com/e/t/etws/authorize?key=...&token=... in their browser, (3) they paste back the verifier code, then exchange for an access token at /oauth/access_token.
- Store the resulting access_token and access_token_secret in memory (not on disk). Track the timestamp of login.
- Expose a function `get_session()` that returns a valid OAuth1 session, and raises an error with a clear "session expired — please re-login" message if it has been more than 115 minutes since login (to give a 5-minute buffer before E*TRADE's 2-hour expiry).
- Write a `main.py` CLI with a `login` subcommand that runs this flow and confirms success.
- Include a `requirements.txt` with pinned versions.
