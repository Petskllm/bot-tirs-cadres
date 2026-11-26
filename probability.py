"""Probabilités prédictives pour des statistiques de comptage.

La loi de Poisson sert de référence lorsque la variance observée ne dépasse
pas la moyenne. Les comptages surdispersés sont modélisés par une binomiale
négative ajustée par les moments. Ce choix est un modèle provisoire, pas une
calibration empirique ni une garantie de rentabilité.
"""
from __future__ import annotations

import math


def _poisson_cdf(k: int, mean: float) -> float:
    if k < 0:
        return 0.0
    term = math.exp(-mean)
    total = term
    for value in range(1, k + 1):
        term *= mean / value
        total += term
    return min(1.0, max(0.0, total))


def _negative_binomial_cdf(k: int, mean: float, variance: float) -> float:
    """CDF d'une NB paramétrée par moyenne et variance (> moyenne)."""
    if k < 0:
        return 0.0
    excess = max(variance - mean, 1e-12)
    shape = mean * mean / excess
    success_probability = shape / (shape + mean)
    probability = math.exp(shape * math.log(success_probability))
    total = probability
    failure_probability = 1.0 - success_probability
    for value in range(1, k + 1):
        probability *= ((value - 1 + shape) / value) * failure_probability
        total += probability
    return min(1.0, max(0.0, total))


def line_probabilities(mean: float, variance: float, line: float, samples: int) -> tuple[float, float]:
    """Retourne P(X > line), P(X <= line) pour un comptage entier.

    Aux lignes .5 usuelles, les deux événements sont complémentaires sans
    égalité/push. Une ligne entière suit la convention stricte X > ligne.
    """
    mean = float(mean)
    variance = float(variance)
    line = float(line)
    if not math.isfinite(mean) or not math.isfinite(variance) or not math.isfinite(line):
        raise ValueError("mean, variance et line doivent être finis")
    if mean <= 0:
        return 0.0, 1.0
    if variance < 0 or samples < 0:
        raise ValueError("variance et samples doivent être positifs")

    cutoff = math.floor(line)
    if variance <= mean:
        under = _poisson_cdf(cutoff, mean)
    else:
        # La variance estimée sur peu d'observations est incertaine; cette
        # correction est limitée à un terme d'erreur standard sur la moyenne.
        predictive_variance = variance + mean / max(1, int(samples))
        under = _negative_binomial_cdf(cutoff, mean, predictive_variance)
    under = min(1.0, max(0.0, under))
    return 1.0 - under, under


def score_probabilities(rows: list[dict]) -> dict:
    """Calcule des scores propres pour les prédictions binaires résolues."""
    values = []
    for row in rows:
        try:
            probability = float(row["probability"])
            outcome = 1.0 if row["won"] else 0.0
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(probability):
            continue
        probability = min(1.0 - 1e-15, max(1e-15, probability))
        values.append((probability, outcome))
    if not values:
        return {"predictions": 0}

    count = len(values)
    mean_probability = sum(p for p, _ in values) / count
    hit_rate = sum(y for _, y in values) / count
    brier = sum((p - y) ** 2 for p, y in values) / count
    log_loss = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in values) / count
    baseline_brier = hit_rate * (1 - hit_rate)
    return {
        "predictions": count,
        "wins": int(sum(y for _, y in values)),
        "hit_rate": round(hit_rate, 4),
        "average_probability": round(mean_probability, 4),
        "calibration_gap": round(hit_rate - mean_probability, 4),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "prevalence_baseline_brier": round(baseline_brier, 4),
        "brier_skill_vs_prevalence": round(1 - brier / baseline_brier, 4) if baseline_brier > 0 else None,
    }
