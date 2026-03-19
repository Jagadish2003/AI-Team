"""EPIC E0 helper: validate that contract mapping is complete.

Checks:
1) every mapped mock file exists (when run from repo root)
2) every src/data/mock*.json file is included in the map (warn on unmapped)
"""

import json
import glob
from pathlib import Path

CONTRACT_MAP = Path("../contracts/mock_to_endpoint_map.json")

def main():
    if not CONTRACT_MAP.exists():
        raise SystemExit("Missing contracts/mock_to_endpoint_map.json")

    mapping = json.loads(CONTRACT_MAP.read_text(encoding="utf-8"))
    # 1) mapped mocks exist
    missing = []
    for mock_path in mapping.keys():
        if not Path("../frontend", mock_path).exists():
            missing.append(mock_path)

    if missing:
        print("WARNING: Mapped mock files not found (run from repo root):")
        for p in missing:
            print(" -", p)
    else:
        print("OK: All mapped mock files exist.")

    # 2) all mocks are mapped
    all_mocks = set(glob.glob("src/data/mock*.json"))
    mapped_mocks = set(mapping.keys())
    unmapped = sorted(all_mocks - mapped_mocks)

    if unmapped:
        print("\nWARNING: Mock files with NO contract entry:")
        for p in unmapped:
            print(" -", p)
    else:
        print("\nOK: All src/data/mock*.json files are mapped.")

    print("\nContract map entries:", len(mapping))
    print("Next step: backend implements listed endpoints with identical response shapes.")

if __name__ == "__main__":
    main()
