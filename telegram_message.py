"""Construction de messages Telegram sans couper les pronostics en plein bloc."""

TELEGRAM_SAFE_LIMIT = 3900
CONTINUATION_HEADER = "🎯 PRONOS TIRS & xG — suite"


def build_telegram_messages(intro, sections, max_chars=TELEGRAM_SAFE_LIMIT):
    """Compose un ou plusieurs messages, en conservant chaque section et sélection.

    ``sections`` contient des couples (titre, éléments). Un élément reste entier;
    en cas de découpe, le message suivant répète le nom du marché.
    """
    if max_chars < 80:
        raise ValueError("max_chars doit être au moins égal à 80")

    messages = []
    current = ""

    def append_block(block, overflow_prefix):
        nonlocal current
        block = str(block).strip()
        if not block:
            return
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_chars:
            current = candidate
            return
        if current:
            messages.append(current)
            current = ""
        prefixed = f"{overflow_prefix}\n\n{block}" if overflow_prefix else block
        if len(prefixed) > max_chars:
            raise ValueError("Un bloc de message dépasse la limite Telegram sûre")
        current = prefixed

    for paragraph in str(intro).split("\n\n"):
        append_block(paragraph, CONTINUATION_HEADER)

    for title, items in sections:
        if not items:
            continue
        title = str(title).strip()
        append_block(title, CONTINUATION_HEADER)
        for item in items:
            append_block(item, f"{CONTINUATION_HEADER}\n\n{title} (suite)")

    if current:
        messages.append(current)
    return messages or [str(intro).strip()]
