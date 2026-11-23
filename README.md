# Bot tirs cadrés, tirs, corners et cartons

Bot de pronostics football utilisant l'API Bzzoiro pour analyser les tirs totaux, les tirs cadrés d'équipes et de joueurs, les corners et les cartons. Les statistiques sont pondérées selon la récence et le côté domicile/extérieur.

## Marchés analysés

- totaux de tirs par match ;
- tirs cadrés des équipes ;
- tirs cadrés des joueurs ;
- corners ;
- cartons.

Les probabilités et cotes affichées par le script sont des **estimations du modèle**, et non des cotes récupérées auprès d'un bookmaker. Elles ne constituent pas une garantie de rentabilité.

## Secrets GitHub Actions

- `BZZOIRO_TOKEN` : jeton d'accès à l'API Bzzoiro.
- `TELEGRAM_TOKEN` : jeton du bot Telegram.
- `CHAT_ID` : identifiant du chat ou canal destinataire.

## Installation et exécution locale

```bash
python -m pip install -r requirements.txt
export BZZOIRO_TOKEN=...
export TELEGRAM_TOKEN=...
export CHAT_ID=...
python test_bzzoiro.py
```

Le script envoie un message Telegram lorsqu'il est exécuté. Ne pas le lancer avec des secrets de production pour un simple test de code.

## GitHub Actions

Le workflow se lance quotidiennement selon sa planification ou manuellement avec `workflow_dispatch`. Il ne se lance plus automatiquement à chaque `push`, afin d'éviter un envoi Telegram accidentel lors d'une modification du dépôt.

## Limites connues et maintenance

Le bot ne conserve actuellement pas d'historique local des sélections ni de rapport de ROI par marché. Les résultats doivent donc être suivis séparément avant d'activer ou de renforcer un marché. Les erreurs d'API sont traitées de manière défensive ; une absence de données peut conduire à l'absence de sélection.
