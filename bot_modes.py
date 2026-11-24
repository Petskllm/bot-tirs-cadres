"""Règles de séparation entre simulation, suivi et publication."""


def should_persist_results(dry_run, track_only):
    """Persiste si la simulation est désactivée ou si le suivi seul est demandé."""
    return not dry_run or track_only


def should_send_telegram(dry_run, track_only):
    """N'envoie Telegram qu'en mode production, jamais en dry-run ou suivi seul."""
    return not dry_run and not track_only
