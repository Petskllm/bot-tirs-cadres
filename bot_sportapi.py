"""Bot de pronostics SportAPI.ai: tirs, tirs cadrés équipe et cartons uniquement."""
from __future__ import annotations
import json, math, os
from datetime import datetime, timezone
from pathlib import Path
import requests
from bot_modes import should_persist_results, should_send_telegram
from model_rules import select_team_sot
from sportapi_provider import API_KEY, api_get, fixtures_on_date, get_team_card_profile, get_team_model_form, get_team_shot_profile, request_usage, sportapi_today
import api_football_provider as api_football
from tracking import build_reports, record_predictions, settle_pending
from workflow_outputs import set_github_output
import sportapi_provider as sportapi

DRY_RUN = os.getenv("DRY_RUN", "1").lower() in {"1", "true", "yes"}
TRACK_ONLY = os.getenv("TRACK_ONLY", "0").lower() in {"1", "true", "yes"}
SOURCE_FALLBACK_ENABLED = os.getenv("ENABLE_SOURCE_FALLBACK", "0").lower() in {"1", "true", "yes"}
PUBLISH_TEAM_SOT = os.getenv("PUBLISH_TEAM_SOT", "0").lower() in {"1", "true", "yes"}
MODEL_VERSION = "v2.6-sportapi-logistic-v1"
MODEL_PATH = Path(__file__).resolve().parent / "data" / "sportapi_models.json"
TRAINED_LEAGUES = {
    "premier league":"E0", "english premier league":"E0",
    "la liga":"SP1", "laliga":"SP1", "spanish la liga":"SP1",
    "bundesliga":"D1", "german bundesliga":"D1",
    "serie a":"I1", "italian serie a":"I1",
    "ligue 1":"F1", "french ligue 1":"F1",
}
CARDS_MIN_PROB, CARDS_MIN_SAMPLES, CARDS_MIN_EXPECTED = 0.65, 6, 4.5
ALLOWED_LEAGUES = {
    "premier league", "english premier league",
    "la liga", "laliga", "spanish la liga",
    "serie a", "italian serie a", "bundesliga", "german bundesliga",
    "ligue 1", "french ligue 1",
    "champions league", "uefa champions league",
    "europa league", "uefa europa league",
}


def poisson_plus_de(lam, k):
    if lam <= 0:
        return 0.0
    return 1 - sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(int(k) + 1))


def line_probabilities(mean, variance, line, samples):
    if mean <= 0:
        return 0.0, 1.0
    if samples < 5:
        over = poisson_plus_de(mean, line)
        return over, 1 - over
    sd = math.sqrt(max(mean, variance, 1.0))
    under = 0.5 * (1 + math.erf((line - mean) / sd / math.sqrt(2)))
    return 1 - under, under


def load_models(path=MODEL_PATH):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("model_version") != "sportapi-logistic-v1":
            return {}
        return payload.get("markets", {})
    except (OSError, ValueError, TypeError):
        return {}


def model_probability(model, features):
    """Inférence logistique identique au pipeline exporté, sans sklearn runtime."""
    names = model["features"]
    raw = [features.get(name) for name in names]
    medians, missing_indices = model["medians"], set(model["indicator_indices"])
    if any(raw[i] is None and medians[i] is None for i in range(len(raw))):
        raise ValueError("Artefact du modèle incomplet: médiane manquante")
    values = [float(value) if value is not None else float(medians[i]) for i, value in enumerate(raw)]
    values.extend(1.0 if raw[i] is None else 0.0 for i in sorted(missing_indices))
    if not (len(values) == len(model["scaler_mean"]) == len(model["scaler_scale"]) == len(model["coefficients"])):
        raise ValueError("Artefact du modèle incohérent")
    z = float(model["intercept"])
    for value, mean, scale, coefficient in zip(values, model["scaler_mean"], model["scaler_scale"], model["coefficients"]):
        z += coefficient * ((value - mean) / (scale or 1.0))
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 709.0)))
    e = math.exp(max(z, -709.0))
    return e / (1.0 + e)


def _league_features(code):
    return {f"league_{item}": float(item == code) for item in ("D1", "E0", "F1", "I1", "SP1")}


def _market_features(market, league_code, home_form, away_form, team_form=None, opponent_form=None):
    league = _league_features(league_code)
    if market == "total_tirs":
        return {**{f"h_{key}": value for key, value in home_form.items()},
                **{f"a_{key}": value for key, value in away_form.items()}, **league}
    if market == "tirs_cadres_equipe":
        return {**{f"team_{key}": value for key, value in (team_form or {}).items()},
                **{f"opp_{key}": value for key, value in (opponent_form or {}).items()}, **league}
    return {**{f"h_{key}": value for key, value in home_form.items()
               if key.startswith(("yellow_", "fouls_"))},
            **{f"a_{key}": value for key, value in away_form.items()
               if key.startswith(("yellow_", "fouls_"))}, **league}


def _bzz_get(endpoint):
    """Règle les anciennes lignes Bzzoiro si le secret existe; sinon les laisse en attente."""
    token = os.getenv("BZZOIRO_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(f"https://sports.bzzoiro.com/api/v2/{endpoint}",
                         headers={"Authorization": f"Token {token}"}, timeout=15)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _send_telegram(text):
    token, chat_id = os.getenv("TELEGRAM_TOKEN"), os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Secrets Telegram manquants pour un envoi live")
    for start in range(0, len(text), 4000):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text[start:start+4000]}, timeout=20)
        r.raise_for_status()


def _league_map():
    payload = api_get("leagues") or {}
    rows = payload.get("data", payload.get("leagues", []))
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    return {row.get("id"): " ".join(str(row.get("name", "")).lower().split())
            for row in rows if isinstance(row, dict) and row.get("id") is not None}


def is_allowed_league(name):
    return " ".join(str(name or "").lower().split()) in ALLOWED_LEAGUES

def _print_api_usage():
    usage = request_usage()
    limit_text = str(usage["limit"]) if usage["limit"] is not None else "aucun plafond configuré"
    print(f"SportAPI GET utilisés: {usage['requests']} / {limit_text}; quota quotidien restant signalé: {usage['daily_remaining']}")
    if usage["limit_reached"]:
        print("ATTENTION: plafond atteint; cette sortie est partielle et ne doit pas être interprétée comme un résultat complet.")

def _top_player_text(profile):
    rows = profile.get("top_card_players", [])
    if not rows:
        return ""
    return "; ".join(f"{name} ({count} jaune(s)/6 derniers matchs)" for name, count in rows if count >= 2)


def main():
    if SOURCE_FALLBACK_ENABLED:
        set_github_output("has_predictions", False)
    use_api_football = os.getenv("API_FOOTBALL_FALLBACK", "0").lower() in {"1", "true", "yes"}
    source = "api_football" if use_api_football else "sportapi"
    if use_api_football and not api_football.API_KEY:
        raise SystemExit("API_FOOTBALL_KEY absente : aucun appel effectué.")
    if not use_api_football and not API_KEY:
        raise SystemExit("SPORTAPI_KEY absent : aucun appel effectué.")
    if not DRY_RUN and not TRACK_ONLY and not all((os.getenv("TELEGRAM_TOKEN"), os.getenv("CHAT_ID"))):
        raise SystemExit("Secrets Telegram manquants; aucun pronostic envoyé.")
    if TRACK_ONLY:
        settled = settle_pending(
            None,
            sportapi_getter=None if use_api_football else api_get,
            api_football_getter=api_football.actuals_for_fixture if use_api_football else None,
        )
        print(f"Pronostics antérieurs réglés: {settled}")
        build_reports()

    if not use_api_football:
        sportapi.reset_collection_errors()
    today = api_football.today() if use_api_football else sportapi_today()
    try:
        matches = api_football.fixtures_on_date(today) if use_api_football else fixtures_on_date(today)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        if SOURCE_FALLBACK_ENABLED:
            print(f"{source}: source indisponible ({str(exc)[:300]}); aucun pronostic conservé.")
            return
        raise
    if not matches:
        provider_name = "API-Football" if use_api_football else "SportAPI"
        provider_error = None if use_api_football else sportapi.LAST_ERROR
        if use_api_football:
            usage = api_football.request_usage()
            if api_football.budget_exhausted() or usage["limit_reached"]:
                print("API-Football n'a pas pu terminer la recherche des fixtures dans le budget autorisé; source ignorée.")
                return
        if provider_error:
            print(f"{provider_name} indisponible pour le {today} ({provider_error}); aucun pronostic généré.")
        else:
            print(f"{provider_name} a répondu sans fixture pour le {today}; aucun pronostic généré.")
        if use_api_football:
            usage = api_football.request_usage()
            print(f"API-Football GET utilisés: {usage['requests']} / {usage['limit']}; restant journalier signalé: {usage['daily_remaining']}")
        else:
            _print_api_usage()
        return
    leagues = {} if use_api_football else _league_map()
    # Le modèle logistique est validé sur l'historique SportAPI uniquement.
    models = {} if use_api_football else load_models()
    picks_total, picks_sot, picks_cards = [], [], []
    predictions = []
    analyzed = ignored = 0
    for match in matches:
        if str(match.get("status", "")).lower() != "scheduled":
            continue
        league_id = match.get("league_id")
        league_name = " ".join(str(match.get("league_name", "")).lower().split()) if use_api_football else leagues.get(league_id, " ".join(str(match.get("league_name", "")).lower().split()))
        league_code = TRAINED_LEAGUES.get(league_name)
        if not is_allowed_league(league_name):
            ignored += 1
            continue
        analyzed += 1
        fid = match.get("id")
        home_id, away_id = match.get("home_id"), match.get("away_id")
        home_name, away_name = match.get("home_team", "Domicile"), match.get("away_team", "Extérieur")
        title = f"{home_name} — {away_name}"
        kickoff = match.get("kickoff_utc") or match.get("datetime") or match.get("date")
        season = match.get("season")
        if use_api_football:
            hp = api_football.get_team_shot_profile(home_id, "home", league_id, today, season)
            ap = api_football.get_team_shot_profile(away_id, "away", league_id, today, season)
        else:
            hp = get_team_shot_profile(home_id, "home", league_id, today)
            ap = get_team_shot_profile(away_id, "away", league_id, today)
        use_candidate = bool(league_code and models)
        if use_api_football:
            home_form = api_football.get_team_model_form(home_id, "home", league_id, today, season)
            away_form = api_football.get_team_model_form(away_id, "away", league_id, today, season)
        else:
            home_form = get_team_model_form(home_id, "home", league_id, today) if use_candidate else {}
            away_form = get_team_model_form(away_id, "away", league_id, today) if use_candidate else {}

        h_shots, a_shots = hp.get("total_shots"), ap.get("total_shots")
        if h_shots and a_shots:
            total = h_shots["average"] + a_shots["average"]
            variance = h_shots["variance"] + a_shots["variance"]
            samples = min(h_shots["samples"], a_shots["samples"])
            if 18 < total < 48:
                over, under = line_probabilities(total, variance, 26.5, samples)
                total_model = models.get("total_tirs") if league_code else None
                if total_model:
                    features = _market_features("total_tirs", league_code, home_form, away_form)
                    over = model_probability(total_model, features)
                    under = 1.0 - over
                else:
                    confidence = min(1.0, samples / 8.0)
                    over = 0.5 + (over - 0.5) * confidence
                    under = 0.5 + (under - 0.5) * confidence
                label, probability = max((("+27", over), ("-27", under)), key=lambda x: x[1])
                odds = round(1 / probability * 1.04, 2) if probability else None
                if samples >= 6 and probability >= 0.65 and odds >= 1.28:
                    picks_total.append(f"⚽ {title}\n  Total attendu : {total:.1f}\n  • +27 tirs : {over:.0%} | -27 tirs : {under:.0%}\n  • Sélection : {label} tirs — {probability:.0%} (Cote estimée : {odds})")
                    predictions.append({"event_id": fid, "source": source, "kickoff": kickoff,
                        "match": title, "market": "total_tirs", "selection": label, "line": 26.5,
                        "direction": "over" if label == "+27" else "under", "probability": probability,
                        "estimated_odds": odds, "samples": samples})

        h_sot, a_sot = hp.get("shots_on_target"), ap.get("shots_on_target")
        if PUBLISH_TEAM_SOT:
            for team_name, team_id, side, profile in ((home_name, home_id, "home", h_sot), (away_name, away_id, "away", a_sot)):
                sot_model = models.get("tirs_cadres_equipe") if league_code else None
                if profile and sot_model:
                    team_form = home_form if side == "home" else away_form
                    opponent_form = away_form if side == "home" else home_form
                    features = _market_features("tirs_cadres_equipe", league_code, home_form, away_form,
                                                 team_form=team_form, opponent_form=opponent_form)
                    model_p = model_probability(sot_model, features)
                    selection = (3.5, model_p) if profile["average"] < 12 and model_p >= 0.65 else None
                else:
                    selection = select_team_sot(profile, line_probabilities) if profile else None
                if selection:
                    line, probability = selection
                    odds = round(1 / probability * 1.04, 2)
                    picks_sot.append(f"🎯 {title}\n  • {team_name} +{line} tirs cadrés : {probability:.0%} (Cote min : {odds})")
                    predictions.append({"event_id": fid, "source": source, "kickoff": kickoff,
                        "match": title, "market": "tirs_cadres_equipe", "selection": f"{team_name} +{line}",
                        "line": line, "direction": "over", "probability": probability,
                        "estimated_odds": odds, "team_id": team_id, "side": side,
                        "samples": profile["samples"], "expected_mean": profile["average"],
                        "expected_variance": profile["variance"]})

        if use_api_football:
            hc = api_football.get_team_card_profile(home_id, "home", league_id, today, season)
            ac = api_football.get_team_card_profile(away_id, "away", league_id, today, season)
        else:
            hc = get_team_card_profile(home_id, "home", league_id, today)
            ac = get_team_card_profile(away_id, "away", league_id, today)
        hy, ay = hc.get("yellow_cards"), ac.get("yellow_cards")
        if hy and ay:
            total_cards = hy["average"] + ay["average"]
            card_samples = min(hy["samples"], ay["samples"])
            fouls = hc.get("fouls"), ac.get("fouls")
            total_fouls = sum(item["average"] for item in fouls if item)
            if CARDS_MIN_EXPECTED <= total_cards < 11 and card_samples >= CARDS_MIN_SAMPLES:
                probability, _ = line_probabilities(total_cards, hy["variance"] + ay["variance"], 4.5, card_samples)
                cards_model = models.get("cartons") if league_code else None
                if cards_model:
                    features = _market_features("cartons", league_code, home_form, away_form)
                    probability = model_probability(cards_model, features)
                odds = round(1 / probability * 1.04, 2) if probability else None
                if probability >= CARDS_MIN_PROB:
                    offender_text = "; ".join(filter(None, (f"{home_name}: {_top_player_text(hc)}", f"{away_name}: {_top_player_text(ac)}")))
                    context = f"\n  Fautes moyennes combinées (6 matchs) : {total_fouls:.1f} — contexte, non inclus dans le calcul"
                    if offender_text:
                        context += f"\n  Joueurs souvent avertis (historique, présence non vérifiée) : {offender_text}"
                    picks_cards.append(f"🟨 {title}\n  Cartons jaunes attendus : {total_cards:.1f}{context}\n  • +4,5 cartons jaunes : {probability:.0%} (Cote estimée : {odds})")
                    predictions.append({"event_id": fid, "source": source, "kickoff": kickoff,
                        "match": title, "market": "cartons", "selection": "+4,5 jaunes", "line": 4.5,
                        "direction": "over", "probability": probability, "estimated_odds": odds,
                        "samples": card_samples, "expected_mean": total_cards,
                        "fouls_context": total_fouls})
    incomplete = (
        (not use_api_football and bool(sportapi.COLLECTION_ERRORS))
    )

    provider_label = "API-FOOTBALL — SECOURS" if use_api_football else "SPORTAPI"
    published_markets = "tirs totaux, cartons jaunes"
    if PUBLISH_TEAM_SOT:
        published_markets = "tirs totaux, tirs cadrés par équipe, cartons jaunes"
    lines = [f"PRONOSTICS {provider_label} — {datetime.now().strftime('%d/%m/%Y')}",
             f"Matchs analysés: {analyzed} | autres ligues ignorées: {ignored}",
             f"Marchés: {published_markets}.", ""]
    for heading, rows in (("TIRS TOTAUX", picks_total), ("TIRS CADRÉS ÉQUIPE", picks_sot), ("CARTONS", picks_cards)):
        if rows:
            lines.extend([heading + ":", "\n\n".join(rows), ""])
    if not (picks_total or picks_sot or picks_cards):
        lines.append("Aucune sélection ne passe les filtres aujourd'hui.")
    message = "\n".join(lines)
    if use_api_football:
        usage = api_football.request_usage()
        print(f"API-Football GET utilisés: {usage['requests']} / {usage['limit']}; restant journalier signalé: {usage['daily_remaining']}")
        budget_exhausted = api_football.budget_exhausted() or usage["limit_reached"]
    else:
        _print_api_usage()
        usage = request_usage()
        budget_exhausted = usage["limit_reached"]
    if budget_exhausted or incomplete:
        if budget_exhausted:
            reason = "budget de requêtes atteint"
        elif not use_api_football and sportapi.COLLECTION_ERRORS:
            reason = "erreur(s) de collecte: " + "; ".join(sportapi.COLLECTION_ERRORS[:3])
        else:
            reason = "fenêtre d’analyse interrompue"
        print(f"Analyse {source} incomplète ({reason}); aucune prédiction conservée ni aucun message Telegram envoyé.")
        return
    has_predictions = bool(predictions)
    if SOURCE_FALLBACK_ENABLED:
        if has_predictions:
            set_github_output("has_predictions", True)
        if not has_predictions:
            print(f"{source}: aucune sélection exploitable; passage au fournisseur suivant.")
            return
    if not should_persist_results(DRY_RUN, TRACK_ONLY):
        print("DRY_RUN actif : aucun message Telegram envoyé, aucune prédiction persistée.")
        print(message)
        return
    if not DRY_RUN and not predictions:
        print("Aucune sélection : aucun enregistrement ni message Telegram envoyé.")
        return
    model_version = "v2.6-api-football-baseline" if use_api_football else MODEL_VERSION
    added = record_predictions(predictions, model_version)
    report = build_reports()
    print(f"Nouvelles sélections enregistrées: {added}; pronostics réglés suivis: {report.get('settled_predictions', 0)}")
    if not should_send_telegram(DRY_RUN, TRACK_ONLY):
        print("Mode suivi seul : aucun message Telegram envoyé.")
        return
    _send_telegram(message)
    print("Message Telegram envoyé.")
    settled = settle_pending(
        sportapi_getter=None if use_api_football else api_get,
        api_football_getter=api_football.actuals_for_fixture if use_api_football else None,
    )
    if settled:
        report = build_reports()
        print(f"Pronostics antérieurs réglés après l’envoi : {settled}; total réglé suivi : {report.get('settled_predictions', 0)}")

if __name__ == "__main__":
    main()
