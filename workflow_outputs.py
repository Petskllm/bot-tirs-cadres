"""Écriture sûre des sorties de workflow GitHub Actions."""
from __future__ import annotations

import os
import re

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_github_output(name: str, value: object) -> bool:
    """Ajoute une sortie simple sur une seule ligne; retourne False hors Actions."""
    if not _KEY_RE.fullmatch(str(name)):
        raise ValueError("Nom de sortie GitHub invalide")
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return False
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("Les sorties GitHub doivent tenir sur une ligne")
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"{name}={text}\n")
    return True
