from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELDS = [
    "market", "entity_id", "name", "threshold", "observations", "mean",
    "estimated_probability", "note",
]


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in FIELDS})
    return destination
