#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=backend
py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
