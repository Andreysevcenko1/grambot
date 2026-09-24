#!/usr/bin/env python3
"""Refresh the bundled TON address labels from ton-studio/ton-labels (MIT).

Usage: python scripts/update_labels.py [output-path]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

SOURCE_URL = "https://raw.githubusercontent.com/shuva10v/ton-labels/build/assets.json"
KEEP_CATEGORIES = {"CEX", "fund", "validator", "liquid-staking", "bridge", "infrastructure", "DEX", "lending"}


def build(lines: list) -> dict:
    addresses = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("category") not in KEEP_CATEGORIES or not row.get("address"):
            continue
        addresses[row["address"].upper()] = [
            row.get("name") or row.get("label") or "",
            row.get("category") or "",
            (row.get("comment") or "")[:60],
        ]
    return {
        "_meta": {
            "source": "https://github.com/ton-studio/ton-labels",
            "license": "MIT",
            "categories": sorted(KEEP_CATEGORIES),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(addresses),
        },
        "addresses": addresses,
    }


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "grambot" / "data" / "ton_labels.json")
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "grambot/0.2"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    data = build(text.splitlines())
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {data['_meta']['count']} labels to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
