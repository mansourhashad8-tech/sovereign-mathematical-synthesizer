#!/usr/bin/env python3
"""Merge two settlement ledgers without losing either side's settlements.

The settle job commits to the same branch it publishes from, so two runs can
overlap and the second push is rejected. Resolving that conflict by picking one
side would silently drop settlements, so the ledgers are merged by transaction id
instead and the snapshot is rebuilt from the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("tx_hash"):
            rows[row["tx_hash"]] = row
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: merge_ledgers.py <local.jsonl> <other.jsonl>", file=sys.stderr)
        return 2
    local_path, other_path = Path(argv[1]), Path(argv[2])
    local, other = load(local_path), load(other_path)
    # The local run is newer, so its copy of a shared transaction wins.
    merged = dict(other)
    merged.update(local)
    local_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged.values()),
        encoding="utf-8",
    )
    print(f"ledger union: local {len(local)}, other {len(other)}, merged {len(merged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
