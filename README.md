# Bot tirs cadrés, tirs, corners et cartons

Bot de pronostics football utilisant l'API Bzzoiro pour analyser les tirs totaux, les tirs cadrés d'équipes et de joueurs, les corners et les cartons. Les statistiques sont pondérées selon la récence et le côté domicile/extérieur.

## Marchés analysés

- totaux de tirs par match ;
- tirs cadrés des équipes ;
- tirs cadrés des joueurs ;
- corners ;
- cartons.

Les probabilités et cotes affichées par le script sont des **estimations du modèle**, et non des cotes récupérées auprès d'un bookmaker. Elles ne constituent pas une garantie de rentabilité.

Pour les tirs totaux, la ligne principale est désormais séparée en deux marchés complémentaires : **plus de 27 tirs** (au moins 27) et **moins de 27 tirs** (au plus 26). Le bot retient le côté dont la probabilité estimée est la plus élevée, sous réserve du seuil de confiance.

Les lignes suivies explicitement sont `+27/-27 tirs`, `+9,5 corners` et `+4,5 cartons`. Le marché `+4,5 cartons` est prioritaire : il exige au moins 65 % de probabilité, au moins 6 observations de cartons pour chaque équipe et un total attendu d'au moins 4,5 cartons. Il n'a pas de cote minimale artificielle. Les tirs cadrés équipes utilisent les lignes `+3,5` et `+4,5`, tandis que les tirs cadrés joueurs utilisent `+0,5` et `+1,5`.

## Secrets GitHub Actions

- `BZZOIRO_TOKEN` : jeton d'accès à l'API Bzzoiro.
- `TELEGRAM_TOKEN` : jeton du bot Telegram.
- `CHAT_ID` : identifiant du chat ou canal destinataire.
- `API_FOOTBALL_KEY` : clé API-Football facultative pour le contrôle secondaire
  des correspondances de matchs. Elle ne doit jamais être écrite dans le code.

## Installation et exécution locale

```bash
python -m pip install -r requirements.txt
export BZZOIRO_TOKEN=...
export TELEGRAM_TOKEN=...
export CHAT_ID=...
python test_bzzoiro.py
```

Le script envoie un message Telegram lorsqu'il est exécuté. Ne pas le lancer avec des secrets de production pour un simple test de code.

Pour analyser les données et afficher le message sans envoyer Telegram, utiliser `DRY_RUN=1`. Ce mode nécessite seulement `BZZOIRO_TOKEN`.

```bash
DRY_RUN=1 BZZOIRO_TOKEN=... python test_bzzoiro.py
```

## Backtest local

Le fichier `backtest_bzzoiro.py` rejoue des matchs terminés sans envoyer de message Telegram. Les historiques de chaque match sont limités aux rencontres antérieures à son coup d'envoi afin d'éviter d'utiliser des données futures. Il produit un CSV détaillé et un résumé JSON par marché.

```bash
export BZZOIRO_TOKEN=...
python backtest_bzzoiro.py \
  --start 2026-10-01 \
  --end 2026-10-31 \
  --max-matches 30 \
  --output backtest_results.csv
```

Le backtest ne prouve pas la rentabilité : il mesure le taux de réussite théorique des sélections qui passent les filtres. Pour les tirs cadrés équipe/joueur, les fenêtres d'historique, profils, conditions d'éligibilité et seuils de sélection sont partagés avec la production. Les autres marchés du backtest restent exploratoires et ne reproduisent pas encore intégralement les règles de production. Les résultats doivent être interprétés séparément par marché et avec le nombre de sélections disponibles. Les historiques d'équipes et de joueurs sont bornés avant le coup d'envoi ; les cotes et prédictions recalculées après le match ne sont volontairement pas injectées dans le score historique, car elles créeraient une fuite de données.

## Données Bzzoiro exploitées

Le bot utilise uniquement Bzzoiro. Il consulte `availability` avant les sous-ressources historiques, puis exploite les statistiques d'équipe, l'xG disponible, les absences, les statistiques joueurs, les compositions confirmées, les prédictions et les cotes consensus. Les compositions confirmées servent à ne retenir que les joueurs titulaires lorsque les identifiants sont disponibles. La prédiction Bzzoiro et la cote consensus servent de confirmation secondaire pour les corners ; elles ne remplacent pas l'historique réel du modèle. Les arbitres et leur moyenne de cartons sont utilisés uniquement pour le marché cartons, avec un échantillon minimal et un ajustement plafonné.

## GitHub Actions

Le workflow est manuel avec `workflow_dispatch` et ne se lance pas automatiquement. Lors d'un lancement manuel, `dry_run` est activé par défaut : le bot interroge l'API et affiche le message sans l'envoyer ni modifier les données suivies. Pour collecter/régler les pronostics sans publier sur Telegram, active `track_only` ; le workflow enregistre alors `data/` dans le dépôt. Désactive `dry_run` sans activer `track_only` uniquement si tu veux publier sur Telegram.

La récupération des matchs du jour Bzzoiro est retentée trois fois. Une réponse vide est distinguée d'une erreur HTTP ou réseau. Si Bzzoiro ne répond pas, API-Football est interrogée pour confirmer si des fixtures existent ; elle ne génère toutefois aucun pronostic de secours tant que les historiques Bzzoiro nécessaires ne sont pas disponibles.

## Limites connues et maintenance

Le bot conserve maintenant chaque sélection réelle dans `data/predictions.jsonl`, puis règle automatiquement les matchs terminés lors des exécutions suivantes. `data/market_report.json` calcule le taux de réussite, l'écart de calibration et le score de Brier par marché ; `data/calibration.json` regroupe les résultats par tranches de probabilité. Le mode `DRY_RUN` ne modifie jamais ces fichiers. Les erreurs d'API sont traitées de manière défensive ; une absence de données peut conduire à l'absence de sélection.

Lorsque `API_FOOTBALL_KEY` est disponible, le bot interroge API-Football en
mode validation uniquement. Il compare les matchs éligibles du jour et affiche
le nombre de correspondances, sans modifier les probabilités Bzzoiro ni bloquer
une sélection. Cette étape permet de mesurer la couverture avant d'utiliser
éventuellement les tirs et tirs cadrés API-Football dans les décisions. Le plan
gratuit est limité à 100 requêtes par jour ; le module effectue actuellement
une seule requête de fixtures par exécution.

Les réponses API sont mises en cache pendant l'exécution afin d'éviter de redemander plusieurs fois les mêmes événements et statistiques. Le modèle des tirs utilise les moyennes pondérées des dix derniers matchs disponibles, séparées domicile/extérieur, ainsi que la variance historique des tirs. Il utilise une approximation normale lorsque l'échantillon est suffisant et revient à Poisson avec peu de données. Les champs `total_shots`, `shots_on_target`, `shots_off_target`, `blocked_shots`, `hit_woodwork` et `xg` sont lus lorsqu'ils sont disponibles ; `xg` reste une information complémentaire et n'est pas converti artificiellement en nombre de tirs. Les absences offensives explicitement identifiées par `availability` sont prises en compte avec une correction plafonnée, tandis qu'une absence sans poste connu est ignorée pour éviter de pénaliser à tort une équipe.

Une calibration empirique par championnat, équipe et cotes réelles nécessitera encore un historique de résultats. Les seuils de confiance doivent donc être réévalués après plusieurs dizaines de matchs résolus. Les lignes corners, cartons et tirs cadrés utilisent maintenant le même profil pondéré et la même gestion de variance que les tirs totaux.


## Nouveau socle Python modulaire

Le paquet `src/bot_tirs_cadres` ajoute un flux de collecte séparé des scripts historiques. Il utilise SQLite pour le cache HTTP, les matchs, statistiques brutes, tirs, incidents, joueurs et profils d'arbitres. La clé reste hors du dépôt : `BZZOIRO_TOKEN` est lue depuis l'environnement. Une configuration de départ se trouve dans `config.yaml.example`.

La documentation Bzzoiro indique un quota football de 7 500 appels par jour, réinitialisé à minuit UTC, et un plafond de 25 requêtes/s sur certains endpoints d'événements. Le client applique une limite conservatrice configurable de 20 req/s, comptabilise les appels réseau localement, réutilise le cache et traite `Retry-After`. Lorsqu'une réponse `429` signale l'épuisement du quota journalier, le programme s'arrête sans attendre jusqu'au lendemain. Le compteur local est une protection supplémentaire : les en-têtes `RateLimit` du serveur restent la source d'autorité.

> Attention : la référence officielle expose les moyennes globales de cartons dans le profil arbitre (`/api/v2/referees/{id}/`). La route `/matches/` renvoie le journal des matchs; le paquet estime les fréquences récentes de cartons à partir des incidents archivés. Ce n'est pas une probabilité prédictive calibrée. Le CSV donne les observations, la moyenne et la fréquence empirique de dépassement; il ne fournit ni cotes de bookmaker ni garantie de rentabilité. Les cotes de cartons ne sont pas nécessaires à cette collecte.

### Installation et commandes

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp config.yaml.example config.yaml
export BZZOIRO_TOKEN="votre_jeton"
PYTHONPATH=src python -m bot_tirs_cadres coverage
PYTHONPATH=src python -m bot_tirs_cadres collect
PYTHONPATH=src python -m bot_tirs_cadres report
PYTHONPATH=src python -m bot_tirs_cadres run
```

`coverage` ne requiert pas de jeton. `collect` récupère les matchs futurs sur l'horizon configuré, puis les statistiques/incidents des matchs terminés sur la période historique configurée. Les appels joueurs sont bornés par `max_player_ids_per_run`; les identifiants `player_ids` configurés sont prioritaires. Pour limiter les appels aux ligues suivies, renseigne `league_ids` dans `config.yaml`. `report` écrit `reports/football_analysis.csv` par défaut; le chemin peut être remplacé par `--csv chemin.csv`.

### Tests

Les tests du nouveau paquet n'appellent pas l'API :

```bash
PYTHONPATH=src pytest -q tests/unit
```

Les scripts et tests historiques à la racine du dépôt restent inchangés. Le nouveau paquet est volontairement ajouté en parallèle afin de permettre une migration progressive.

Références utilisées pour les routes et limites : [conventions et quotas Bzzoiro](https://sports.bzzoiro.com/docs/conventions/), [événements, statistiques et incidents](https://sports.bzzoiro.com/docs/football/events/), [statistiques joueurs](https://sports.bzzoiro.com/docs/football/teams-players/) et [arbitres](https://sports.bzzoiro.com/docs/football/managers-referees-venues/).
