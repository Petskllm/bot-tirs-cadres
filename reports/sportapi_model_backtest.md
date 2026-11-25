# Backtest du modèle SportAPI — tirs et cartons

Le backtest walk-forward compare les règles probabilistes actuelles à une régression logistique régularisée pour les trois marchés pris en charge par SportAPI : total de tirs, tirs cadrés d’équipe et cartons jaunes. Les statistiques équipe utilisées sont `shots`, `shots_on_target`, `yellow_cards` et `fouls`; le bot ne dépend ni de corners ni de tirs cadrés individuels, absents des endpoints documentés retenus. Les endpoints de stats et d’événements sont en lecture seule. [1]

L’évaluation utilise 14 358 matchs complets de cinq ligues (Angleterre, Espagne, Allemagne, Italie et France), sur huit saisons de 2017/18 à 2024/25. Pour chaque saison testée, le modèle est ajusté uniquement sur les saisons antérieures; tous les matchs d’une même date sont prédits avant d’ajouter leurs résultats à l’historique. Les variables sont les moyennes pondérées des formes équipe à domicile/extérieur sur les cinq et dix matchs antérieurs, plus l’indicateur de ligue. L’imputation médiane, la standardisation et la logistique avec régularisation C=0,1 sont réajustées dans chaque pli. Les fichiers Football-Data fournissent les résultats et statistiques de match utilisés pour ces cibles. [2]

Sur les sept saisons testées hors entraînement (2018/19–2024/25), le candidat a obtenu un Brier et un log-loss plus faibles que les règles actuelles pour chacun des trois marchés, à chaque saison testée :

- **Total de tirs (27 ou plus)** : 11 774 pronostics évalués. Brier 0,24524 avec la règle actuelle contre 0,21817 avec la logistique; log-loss 0,69737 contre 0,62653.
- **Tirs cadrés d’équipe (4 ou plus)** : 24 962 pronostics. Brier 0,23187 contre 0,21563; log-loss 0,65866 contre 0,61938.
- **Cartons jaunes (5 ou plus)** : 12 229 pronostics. Brier 0,24443 contre 0,22763; log-loss 0,69616 contre 0,64724.

Cela justifie de traiter cette logistique comme un **candidat à tester**, pas comme un modèle définitivement supérieur dans toutes les conditions. Le test compare une seule famille de modèle aux règles actuelles; il ne prouve pas qu’il s’agit du meilleur modèle possible. Le jeu de données ne contient pas les cotes correspondantes et aucun ROI n’a été calculé. Une amélioration de Brier ou de log-loss ne garantit donc ni rentabilité ni gains. La qualité des statistiques historiques peut aussi différer des réponses SportAPI actuelles.

Le bot applique l’artefact exporté aux cinq ligues couvertes. Pour les autres compétitions autorisées, il conserve explicitement les règles précédentes. Les seuils de sélection existants restent en place; pour les tirs cadrés des ligues couvertes, le candidat correspond uniquement au marché équipe **plus de 3,5**. Le mode par défaut demeure `DRY_RUN=1`, sans envoi Telegram ni enregistrement de pronostics. Aucun run réel SportAPI ou workflow GitHub n’a été exécuté dans le cadre de cette modification.

## Réentraînement

Dans le dépôt, la commande suivante installe les dépendances d’analyse, télécharge les mêmes huit saisons, recalcule l’évaluation walk-forward et exporte les coefficients finaux dans l’artefact utilisé par le bot :

```bash
python3 -m pip install -r requirements-model.txt
```

Puis :

```bash
python3 sportapi_model_backtest.py \
  --seasons 1718 1819 1920 2021 2122 2223 2324 2425 \
  --leagues E0 SP1 D1 I1 F1 \
  --output /tmp/sportapi_model_backtest.json \
  --export-model data/sportapi_models.json
```

Le script d’analyse requiert scikit-learn; le bot en production ne le requiert pas, car il utilise directement les coefficients exportés.

## Références

[1]: https://sportapi.ai/docs "Documentation officielle SportAPI.ai"
[2]: https://www.football-data.co.uk/ "Football-Data.co.uk, résultats et statistiques historiques de football"
