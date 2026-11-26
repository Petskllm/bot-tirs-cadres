from __future__ import annotations

import argparse
import json
import logging
from typing import Sequence

from bot_tirs_cadres.analysis.metrics import build_report_rows
from bot_tirs_cadres.api.bzzoiro import BzzoiroAPI
from bot_tirs_cadres.api.client import BzzoiroHTTPClient
from bot_tirs_cadres.collectors.collector import collect_data
from bot_tirs_cadres.config import load_config
from bot_tirs_cadres.reports.csv_report import write_csv
from bot_tirs_cadres.storage.database import SQLiteStore


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collecte et analyse des statistiques football Bzzoiro.")
    parser.add_argument("command", choices=("coverage", "collect", "report", "run"))
    parser.add_argument("--config", default="config.yaml", help="Chemin du fichier YAML")
    parser.add_argument("--csv", help="Chemin CSV facultatif (remplace report.csv_path)")
    parser.add_argument("--verbose", action="store_true", help="Activer les logs de débogage")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config, require_token=args.command in ("collect", "run"))
    store = SQLiteStore(cfg.cache_path)
    client = BzzoiroHTTPClient(cfg, store)
    api = BzzoiroAPI(client)

    if args.command == "coverage":
        print(json.dumps(api.coverage(), ensure_ascii=False, indent=2))
        return 0
    if args.command in ("collect", "run"):
        print(json.dumps(collect_data(api, store, cfg), ensure_ascii=False, indent=2))
    if args.command in ("report", "run"):
        rows = build_report_rows(store, cfg)
        target = write_csv(rows, args.csv or cfg.report_path)
        print(f"Rapport CSV : {target} ({len(rows)} lignes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
