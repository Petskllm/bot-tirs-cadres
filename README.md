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

Pour analyser les données et afficher le message sans envoyer Telegram, utiliser `DRY_RUN=1`. Ce mode nécessite seulement `BZZOIRO_TOKEN`.

```bash
DRY_RUN=1 BZZOIRO_TOKEN=... python test_bzzoiro.py
```

## GitHub Actions

Le workflow se lance quotidiennement selon sa planification ou manuellement avec `workflow_dispatch`. Il ne se lance plus automatiquement à chaque `push`, afin d'éviter un envoi Telegram accidentel lors d'une modification du dépôt. Lors d'un lancement manuel, l'option `dry_run` permet d'interroger l'API et d'afficher le message sans l'envoyer.

## Limites connues et maintenance

Le bot ne conserve actuellement pas d'historique local des sélections ni de rapport de ROI par marché. Les résultats doivent donc être suivis séparément avant d'activer ou de renforcer un marché. Les erreurs d'API sont traitées de manière défensive ; une absence de données peut conduire à l'absence de sélection.

Les réponses API sont mises en cache pendant l'exécution afin d'éviter de redemander plusieurs fois les mêmes événements et statistiques. Le modèle des tirs utilise les moyennes pondérées des dix derniers matchs disponibles, séparées domicile/extérieur, ainsi que la variance historique des tirs. Il utilise une approximation normale lorsque l'échantillon est suffisant et revient à Poisson avec peu de données. Les champs `total_shots`, `shots_on_target`, `shots_off_target`, `blocked_shots`, `hit_woodwork` et `xg` sont lus lorsqu'ils sont disponibles ; `xg` reste une information complémentaire et n'est pas converti artificiellement en nombre de tirs.

Une calibration empirique par championnat, équipe et cotes réelles nécessitera encore un historique de résultats. Les seuils de confiance doivent donc être réévalués après plusieurs dizaines de matchs résolus.
