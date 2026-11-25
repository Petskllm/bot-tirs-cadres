"""Suivi local des pronostics et calibration empirique par marché."""
from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
PREDICTIONS_FILE = DATA_DIR / "predictions.jsonl"
REPORT_FILE = DATA_DIR / "market_report.json"
CALIBRATION_FILE = DATA_DIR / "calibration.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                continue
    return rows


def _write_json(path, value):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prediction_key(row):
    return "|".join(
        str(row.get(field, ""))
        for field in ("source", "event_id", "market", "selection", "team_id", "player_id", "model_version")
    )


def record_predictions(rows, model_version):
    """Ajoute uniquement les nouvelles sélections; ne fait rien pour DRY_RUN."""
    if not rows:
        return 0
    existing = _read_jsonl(PREDICTIONS_FILE)
    known = {_prediction_key(row) for row in existing}
    added = 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with PREDICTIONS_FILE.open("a", encoding="utf-8") as handle:
        for row in rows:
            row = dict(row)
            row.setdefault("model_version", model_version)
            row.setdefault("status", "pending")
            row.setdefault("created_at", now_iso())
            key = _prediction_key(row)
            if key in known:
                continue
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            known.add(key)
            added += 1
    return added


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _actuals(getter, event_id):
    stats = getter(f"events/{event_id}/stats/") or {}
    sides = stats.get("stats", {})
    home, away = sides.get("home", {}), sides.get("away", {})
    values = {}
    for name, key in (("total_tirs", "total_shots"), ("corners", "corner_kicks"), ("cartons", "yellow_cards")):
        home_value, away_value = _number(home.get(key)), _number(away.get(key))
        if home_value is not None and away_value is not None:
            values[name] = home_value + away_value
    values["home_sot"] = _number(home.get("shots_on_target"))
    values["away_sot"] = _number(away.get("shots_on_target"))
    player_stats = getter(f"events/{event_id}/player-stats/") or {}
    values["players"] = {
        str(player.get("player_id")): _number(player.get("shots_on_target"))
        for player in player_stats.get("player_stats", [])
        if player.get("player_id") and _number(player.get("shots_on_target")) is not None
    }
    return values


def _sportapi_actuals(getter, fixture_id):
    """Lit uniquement les champs documentés de stats équipe SportAPI."""
    payload = getter(f"fixtures/{fixture_id}/stats") or {}
    sides = payload.get("data", {}) if isinstance(payload, dict) else {}
    home, away = sides.get("home", {}), sides.get("away", {})
    values = {}
    for name, key in (("total_tirs", "shots"), ("cartons", "yellow_cards")):
        home_value, away_value = _number(home.get(key)), _number(away.get(key))
        if home_value is not None and away_value is not None:
            values[name] = home_value + away_value
    values["home_sot"] = _number(home.get("shots_on_target"))
    values["away_sot"] = _number(away.get("shots_on_target"))
    return values


def _api_football_actuals(getter, fixture_id):
    """Récupère les valeurs déjà normalisées par le fournisseur API-Football."""
    values = getter(fixture_id) or {}
    return values if isinstance(values, dict) else {}


def _won(row, actuals):
    market = row.get("market")
    selection = str(row.get("selection", ""))
    line = _number(row.get("line"))
    if line is None:
        return None, None
    if market == "total_tirs":
        actual = actuals.get("total_tirs")
    elif market == "corners":
        actual = actuals.get("corners")
    elif market == "cartons":
        actual = actuals.get("cartons")
    elif market == "tirs_cadres_equipe":
        actual = actuals.get("home_sot" if row.get("side") == "home" else "away_sot")
    elif market == "tirs_cadres_joueur":
        actual = actuals.get("players", {}).get(str(row.get("player_id")))
    else:
        return None, None
    if actual is None:
        return None, None
    direction = row.get("direction", "over")
    won = actual > line if direction == "over" else actual <= line
    return actual, bool(won)


def settle_pending(getter=None, sportapi_getter=None, api_football_getter=None):
    """Règle les lignes uniquement avec leur fournisseur d'origine."""
    rows = _read_jsonl(PREDICTIONS_FILE)
    changed = 0
    for row in rows:
        if row.get("status") != "pending":
            continue
        source = row.get("source")
        if source == "sportapi" and sportapi_getter:
            actuals = _sportapi_actuals(sportapi_getter, row.get("event_id"))
        elif source == "api_football" and api_football_getter:
            actuals = _api_football_actuals(api_football_getter, row.get("event_id"))
        else:
            continue
        actual, won = _won(row, actuals)
        if actual is None:
            continue
        row["actual_value"] = actual
        row["won"] = won
        row["status"] = "settled"
        row["settled_at"] = now_iso()
        changed += 1
    if changed:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with PREDICTIONS_FILE.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return changed


def _bucket(probability):
    probability = max(0, min(0.99, float(probability)))
    lower = int(probability * 10) * 10
    return f"{lower:02d}-{lower + 9:02d}%"


def build_reports():
    rows = [row for row in _read_jsonl(PREDICTIONS_FILE) if row.get("status") == "settled"]
    market_groups = defaultdict(list)
    buckets = defaultdict(list)
    for row in rows:
        try:
            probability = float(row["probability"])
            won = 1 if row["won"] else 0
        except (KeyError, TypeError, ValueError):
            continue
        provider_market = f"{row.get('source', 'bzzoiro')}:{row.get('market', 'unknown')}"
        market_groups[provider_market].append((probability, won))
        buckets[(provider_market, _bucket(probability))].append((probability, won))

    report = {"generated_at": now_iso(), "settled_predictions": len(rows), "markets": {}}
    for market, values in sorted(market_groups.items()):
        report["markets"][market] = {
            "predictions": len(values),
            "wins": sum(won for _, won in values),
            "hit_rate": round(sum(won for _, won in values) / len(values), 4),
            "average_probability": round(sum(prob for prob, _ in values) / len(values), 4),
            "calibration_gap": round(sum(won - prob for prob, won in values) / len(values), 4),
            "brier_score": round(sum((prob - won) ** 2 for prob, won in values) / len(values), 4),
        }
    calibration = {"generated_at": report["generated_at"], "markets": {}}
    for (market, bucket), values in sorted(buckets.items()):
        calibration["markets"].setdefault(market, {})[bucket] = {
            "predictions": len(values),
            "hit_rate": round(sum(won for _, won in values) / len(values), 4),
            "average_probability": round(sum(prob for prob, _ in values) / len(values), 4),
        }
    _write_json(REPORT_FILE, report)
    _write_json(CALIBRATION_FILE, calibration)
    return report
