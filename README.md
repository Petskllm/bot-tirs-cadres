# Bot de pronostics football

Le bot analyse les matchs de football des cinq grands championnats européens, de la Ligue des champions et de l’Europa League. Il examine les tirs totaux, les corners, les cartons, les tirs cadrés par équipe et les tirs cadrés par joueur. Les marchés de tirs cadrés sont actifs pour commencer à enregistrer leurs résultats.

Les probabilités et cotes affichées sont des estimations du modèle, pas des cotes de bookmaker ni une garantie de rentabilité.

## Lancer les pronostics

Dans GitHub Actions, lance uniquement le workflow **Pronostics Telegram** sur la branche `main`. Il utilise les fournisseurs en cascade : Bzzoiro, puis API-Football, puis SportAPI si les précédents ne donnent pas de sélection exploitable. Le workflow envoie les pronostics dans Telegram et enregistre automatiquement les sélections envoyées.

Secrets utilisés :

- `BZZOIRO_TOKEN` : accès à Bzzoiro;
- `API_FOOTBALL_KEY` : fournisseur de secours et validation;
- `SPORTAPI_KEY` : fournisseur de dernier recours;
- `TELEGRAM_TOKEN` et `CHAT_ID` : envoi dans Telegram.

Les clés sont à conserver dans les secrets GitHub Actions, jamais dans le code du dépôt.

## Historique et évaluation

Les sélections envoyées sont inscrites dans `data/predictions.jsonl`, dont les tirs cadrés joueurs et équipe, puis réglées lors de runs ultérieurs après la fin des matchs. Les pronostics joueurs Bzzoiro ne sont proposés que si une composition actuelle ou une dernière composition officielle est disponible et si les critères historiques du modèle sont satisfaits. Les rapports `data/market_report.json` et `data/calibration.json` suivent les résultats et la calibration par marché et par fournisseur. Le workflow de pronostics pousse automatiquement les mises à jour de `data/` sur `main`.

Une sélection identique envoyée lors de deux exécutions différentes est enregistrée pour chacune, afin de garder un historique fidèle des runs. Les pronostics récents ne sont pas réglés trop tôt.

## Tests automatiques

Les tests hors ligne s’exécutent automatiquement à chaque push ou pull request vers `main`. Ils n’envoient aucun message Telegram et ne font aucun appel aux fournisseurs.

Pour les exécuter localement :

```bash
python -m pip install -r requirements.txt
python -m unittest -v test_pure_functions.py test_bot_modes.py test_sportapi_bot.py test_sportapi_dry_run_preflight.py test_api_football_fallback.py test_tracking_api_football.py test_probability_model.py test_provider_cascade.py
```
