import os

def warn(msg: str) -> None:
    if os.getenv("DISCOVERY_SILENCE_WARNINGS") == "1":
        return
    print(f"[WARN] {msg}")

def info(msg: str) -> None:
    print(f"[INFO] {msg}")
