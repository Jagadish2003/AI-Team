#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=backend
python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
