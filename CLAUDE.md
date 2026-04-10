# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`deuce-aureum` is a Python project (based on `.gitignore` configuration). 
etrade-tool is  a wrapper to the official Etrade API

## Setup

```bash
source .venv/bin/activate
pip install -r trading-assistant/requirements.txt
```

Always activate the venv before running `python`, `pip`, or `pytest`.

## Running tests

```bash
source .venv/bin/activate
cd trading-assistant
python -m pytest tests/
```

## CLI usage (trading-assistant)

```bash
cd trading-assistant
python main.py login
python main.py kickoff --headlines headlines/2026-04-10.txt --positions positions.txt
python main.py kickoff --skip-auth --headlines headlines/2026-04-10.txt --positions positions.txt
python main.py report AAPL MSFT
python main.py watch AAPL MSFT
python main.py web
```

The database defaults to `./data/trading.db`. Override with `DB_PATH` in `.env`.
