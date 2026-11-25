"""Contrôle GET-only du quota SportAPI avant le dry-run borné."""
from __future__ import annotations

import os
import sys

import requests

BASE_URL = "https://sportapi.ai/api"
DAILY_NAMES = (
    "x-ratelimit-remaining-daily", "x-ratelimit-remaining-day",
    "x-rate-limit-remaining-daily", "x-rate-limit-remaining-day",
    "ratelimit-remaining-daily", "rate-limit-remaining-daily",
    "x-daily-remaining", "daily-remaining", "x-ratelimit-remaining",
    "x-rate-limit-remaining", "ratelimit-remaining", "rate-limit-remaining",
)
MONTHLY_NAMES = (
    "x-ratelimit-remaining-monthly", "x-ratelimit-remaining-month",
    "x-rate-limit-remaining-monthly", "x-rate-limit-remaining-month",
    "ratelimit-remaining-monthly", "rate-limit-remaining-monthly",
    "x-monthly-remaining", "monthly-remaining",
)


def remaining_from_headers(headers, names):
    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    for name in names:
        if name in lowered:
            try:
                return max(0, int(float(lowered[name])))
            except ValueError:
                continue
    return None


def choose_budget(daily_remaining, monthly_remaining=None):
    """Gardes 20 requêtes de marge; plafonne le bot à 100 GET."""
    if daily_remaining is None:
        raise ValueError("SportAPI n'a pas fourni d'en-tête de quota journalier; arrêt par sécurité.")
    if daily_remaining < 120:
        raise ValueError(f"Quota journalier restant insuffisant ({daily_remaining}); minimum requis: 120.")
    if monthly_remaining is not None and monthly_remaining < 120:
        raise ValueError(f"Quota mensuel restant insuffisant ({monthly_remaining}); minimum requis: 120.")
    return min(100, daily_remaining - 20)


def main():
    key = os.getenv("SPORTAPI_KEY", "").strip()
    if not key:
        raise SystemExit("SPORTAPI_KEY absent; aucun appel effectué.")
    try:
        response = requests.get(
            f"{BASE_URL}/leagues",
            headers={"X-Api-Key": key},
            timeout=20,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise SystemExit("Échec réseau du contrôle de quota; aucun dry-run effectué.") from None
    if response.status_code != 200:
        raise SystemExit(f"Contrôle SportAPI en échec (HTTP {response.status_code}); aucun dry-run effectué.")
    try:
        payload = response.json()
    except ValueError:
        raise SystemExit("Réponse de contrôle invalide; aucun dry-run effectué.") from None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise SystemExit("Réponse SportAPI non confirmée; aucun dry-run effectué.")

    daily = remaining_from_headers(response.headers, DAILY_NAMES)
    monthly = remaining_from_headers(response.headers, MONTHLY_NAMES)
    try:
        budget = choose_budget(daily, monthly)
    except ValueError as exc:
        raise SystemExit(f"{exc} Un seul GET de contrôle a été effectué; bot non lancé.") from None

    print(f"Préflight accepté: quota quotidien restant après contrôle={daily}; quota mensuel={monthly}; plafond bot={budget} GET; marge minimale conservée=20.")
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"SPORTAPI_MAX_REQUESTS={budget}\n")
    else:
        print(f"SPORTAPI_MAX_REQUESTS={budget}")


if __name__ == "__main__":
    main()
