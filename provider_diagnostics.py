"""Résumé d’erreurs fournisseur sans recopier les valeurs de payload sensibles."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(headers) -> float | None:
    """Convertit Retry-After numérique ou HTTP-date en secondes, si fourni."""
    raw = next(
        (str(value).strip() for key, value in (headers or {}).items()
         if str(key).lower() == "retry-after"),
        None,
    )
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _collect_keys_and_text(value, keys, parts, depth=0):
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(re.sub(r"[^A-Za-z0-9_-]", "", str(key))[:32])
            _collect_keys_and_text(item, keys, parts, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:20]:
            _collect_keys_and_text(item, keys, parts, depth + 1)
    elif isinstance(value, (str, int, float, bool)):
        parts.append(str(value)[:160])


def summarize_http_error(provider: str, status: int, headers=None, payload=None, text="") -> str:
    """Retourne statut, catégorie, noms de champs et Retry-After, sans valeurs brutes."""
    keys: set[str] = set()
    parts: list[str] = []
    _collect_keys_and_text(payload, keys, parts)
    if text:
        parts.append(str(text)[:300])
    signals = " ".join(parts).lower()
    key_signals = " ".join(keys).lower()
    if status == 429 or any(term in signals + " " + key_signals for term in
                            ("rate limit", "ratelimit", "quota", "too many", "requests limit")):
        category = "quota/rate-limit"
    elif any(term in signals + " " + key_signals for term in
             ("token", "api key", "apikey", "authorization", "credential", "invalid key")):
        category = "authentification/clé"
    elif any(term in signals + " " + key_signals for term in
             ("subscription", "plan", "endpoint", "access denied", "not allowed")):
        category = "plan/accès"
    elif status >= 500:
        category = "erreur serveur fournisseur"
    else:
        category = "erreur fournisseur"

    retry_seconds = retry_after_seconds(headers)
    retry = f"{retry_seconds:.0f}s" if retry_seconds is not None else "absent/non valide"
    field_source = payload.get("errors", payload) if isinstance(payload, dict) else None
    error_keys: set[str] = set()
    if isinstance(field_source, dict):
        error_keys = {re.sub(r"[^A-Za-z0-9_-]", "", str(key))[:32]
                      for key in field_source.keys()}
    fields = ",".join(sorted(k for k in error_keys if k)[:8]) or "non détaillés"
    return (f"{provider} HTTP {int(status)}; catégorie={category}; "
            f"champs={fields}; Retry-After={retry}")
