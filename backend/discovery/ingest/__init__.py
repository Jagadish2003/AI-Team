"""
Ingestion package.
INGEST_MODE environment variable controls data source:
  offline  (default) — reads from fixtures/*.json
  live               — calls real APIs using environment credentials
"""
import os

INGEST_MODE = os.getenv("INGEST_MODE", "offline").lower()

def is_live() -> bool:
    return INGEST_MODE == "live"
