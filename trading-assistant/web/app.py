"""
Flask web UI (stub).
"""

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    return "<h1>Trading Assistant</h1><p>Web UI coming soon.</p>"


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug)
