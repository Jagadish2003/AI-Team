"""
Ingestion package.
INGEST_MODE environment variable controls data source:
  offline  (default) — reads from fixtures/*.json
  live               — calls real APIs using environment credentials
"""
import os

def is_live() -> bool:
    INGEST_MODE = os.getenv("INGEST_MODE", "").strip().lower()
    IS_LIVE = (INGEST_MODE == "live")
    return IS_LIVE
