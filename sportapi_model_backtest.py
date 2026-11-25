"""Backtest walk-forward against free Football-Data CSVs; no future leakage."""
from __future__ import annotations
import argparse, json, math
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import brier_score_loss, log_loss

BASE = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
DEFAULT_LEAGUES = ["E0", "SP1", "D1", "I1", "F1"]
STATS = ("shots", "sot", "yellow", "fouls")
COLUMNS = {"shots": ("HS", "AS"), "sot": ("HST", "AST"), "yellow": ("HY", "AY"), "fouls": ("HF", "AF")}

def line_probability(mean, variance, line, samples):
    if mean <= 0: return 0.0
    if samples < 5:
        return 1 - sum(math.exp(-mean) * mean**i / math.factorial(i) for i in range(int(line) + 1))
    sd = max(mean, variance, 1.0) ** 0.5
    under = 0.5 * (1 + math.erf((line - mean) / sd / 2**0.5))
    return 1 - under

def load_seasons(codes, leagues=DEFAULT_LEAGUES):
    pieces = []
    for code in codes:
        for league in leagues:
            frame = pd.read_csv(BASE.format(season=code, league=league), encoding="latin1")
            needed = ["Date", "HomeTeam", "AwayTeam", *[c for pair in COLUMNS.values() for c in pair]]
            if not set(needed).issubset(frame.columns):
                raise ValueError(f"{code}/{league}: CSV sans tous les champs requis")
            frame["date"] = pd.to_datetime(frame["Date"], dayfirst=True, errors="coerce")
            for col in needed[3:]: frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.dropna(subset=needed + ["date"]).copy()
            frame["season"], frame["league"] = code, league
            pieces.append(frame)
            print(f"Saison {code} {league}: {len(frame)} matchs complets", flush=True)
    data = pd.concat(pieces, ignore_index=True)
    return data.sort_values(["date", "season", "HomeTeam", "AwayTeam"], kind="stable").reset_index(drop=True)

def _weighted(rows, key, preferred_side, window):
    values = []
    for idx, row in enumerate(reversed(rows[-window:])):
        value = row.get(key)
        if value is None: continue
        weight = max(1.0, 1.6 - idx * 0.12) * (1.85 if row["side"] == preferred_side else 0.65)
        values.append((float(value), weight))
    if not values: return None
    total = sum(w for _, w in values)
    avg = sum(v*w for v,w in values) / total
    var = sum(w*(v-avg)**2 for v,w in values) / total
    return {"average": avg, "variance": var, "samples": len(values)}

def _form(history, team, side):
    result, rows = {}, history.get(team, [])
    for stat in STATS:
        for direction in ("for", "against"):
            for window in (5, 10):
                profile = _weighted(rows, f"{stat}_{direction}", side, window)
                result[f"{stat}_{direction}_{window}"] = profile["average"] if profile else np.nan
    return result


def _profile(history, team, side, stat, window):
    return _weighted(history.get(team, []), f"{stat}_for", side, window)

def _prob(mean, variance, samples, line):
    return line_probability(mean, variance, line, samples) if mean is not None else None

def _build_rows(data):
    history = defaultdict(list)
    markets = {"total_tirs": [], "tirs_cadres_equipe": [], "cartons": []}
    league_codes = sorted(data["league"].unique())
    # Tous les matchs d'une même date sont prédits avant que leurs résultats entrent dans l'historique.
    for day, group in data.groupby(data["date"].dt.date, sort=True):
        batch = []
        for _, game in group.iterrows():
            league = str(game.league)
            home, away = f"{league}|{game.HomeTeam}", f"{league}|{game.AwayTeam}"
            hf, af = _form(history, home, "home"), _form(history, away, "away")
            feat = {**{f"h_{k}": v for k,v in hf.items()}, **{f"a_{k}": v for k,v in af.items()},
                    **{f"league_{code}": float(code == league) for code in league_codes}}
            hs, ass = _profile(history, home, "home", "shots", 10), _profile(history, away, "away", "shots", 10)
            if hs and ass and min(hs["samples"], ass["samples"]) >= 6:
                mean = hs["average"] + ass["average"]
                if 18 < mean < 48:
                    p = _prob(mean, hs["variance"]+ass["variance"], min(hs["samples"],ass["samples"]), 26.5)
                    conf = min(1.0, min(hs["samples"],ass["samples"])/8.0)
                    p = 0.5 + (p-0.5)*conf
                    markets["total_tirs"].append({"season":game.season,"date":str(day),"features":feat,"baseline":p,"target":int(float(game.HS)+float(game.AS)>=27)})
            hst, ast = _profile(history, home, "home", "sot", 10), _profile(history, away, "away", "sot", 10)
            if hst and ast:
                for side, team, prof, key, opponent in (("h",home,hst,"HST",away),("a",away,ast,"AST",home)):
                    p = _prob(prof["average"], prof["variance"], prof["samples"], 3.5)
                    team_side = "home" if side == "h" else "away"
                    opp_side = "away" if side == "h" else "home"
                    team_form, opp_form = _form(history,team,team_side), _form(history,opponent,opp_side)
                    x = {**{f"team_{k}":v for k,v in team_form.items()},
                         **{f"opp_{k}":v for k,v in opp_form.items()},
                         **{k:v for k,v in feat.items() if k.startswith("league_")}}
                    markets["tirs_cadres_equipe"].append({"season":game.season,"date":str(day),"features":x,"baseline":p,"target":int(float(game[key])>=4)})
            hy, ay = _profile(history, home, "home", "yellow", 6), _profile(history, away, "away", "yellow", 6)
            if hy and ay and min(hy["samples"],ay["samples"]) >= 6:
                mean = hy["average"] + ay["average"]
                p = _prob(mean, hy["variance"]+ay["variance"], min(hy["samples"],ay["samples"]), 4.5)
                x = {k:v for k,v in feat.items() if "yellow" in k or "fouls" in k or k.startswith("league_")}
                markets["cartons"].append({"season":game.season,"date":str(day),"features":x,"baseline":p,"target":int(float(game.HY)+float(game.AY)>=5)})
            batch.append((home,away,game))
        for home, away, game in batch:
            for team, side in ((home,"home"),(away,"away")):
                item = {"side":side}
                for stat,(hcol,acol) in COLUMNS.items():
                    own = float(game[hcol if team==home else acol])
                    opp = float(game[acol if team==home else hcol])
                    item[f"{stat}_for"], item[f"{stat}_against"] = own, opp
                history[team].append(item)
    return markets


def _feature_frame(rows):
    columns = sorted({name for row in rows for name in row["features"]})
    return pd.DataFrame([{name:row["features"].get(name,np.nan) for name in columns} for row in rows],columns=columns)

def _model():
    return make_pipeline(SimpleImputer(strategy="median",add_indicator=True),StandardScaler(),
                         LogisticRegression(C=0.1,max_iter=2000,solver="lbfgs"))

def export_models(data, output_path):
    """Entraîne sur tout l'historique et exporte des coefficients sans sklearn runtime."""
    artifacts = {"model_version":"sportapi-logistic-v1", "seasons":sorted(data["season"].unique()),
                 "leagues":sorted(data["league"].unique()), "markets":{}}
    for market, rows in _build_rows(data).items():
        frame = _feature_frame(rows)
        pipeline = _model().fit(frame,[row["target"] for row in rows])
        imputer = pipeline.named_steps["simpleimputer"]
        scaler = pipeline.named_steps["standardscaler"]
        classifier = pipeline.named_steps["logisticregression"]
        artifacts["markets"][market] = {
            "features":list(frame.columns),
            "medians":[float(x) if np.isfinite(x) else None for x in imputer.statistics_],
            "indicator_indices":[int(x) for x in imputer.indicator_.features_],
            "scaler_mean":[float(x) for x in scaler.mean_],
            "scaler_scale":[float(x) for x in scaler.scale_],
            "coefficients":[float(x) for x in classifier.coef_[0]],
            "intercept":float(classifier.intercept_[0]),
            "training_rows":len(rows),
            "positive_rate":round(float(np.mean([row["target"] for row in rows])),6),
        }
    path=Path(output_path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(artifacts,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print(f"Modèles exportés: {path}")
    return artifacts

def _scores(y, p):
    y, p = np.asarray(y,dtype=int), np.clip(np.asarray(p,dtype=float),1e-6,1-1e-6)
    picks = p >= 0.65
    return {"n":int(len(y)),"brier":round(float(brier_score_loss(y,p)),5),
            "log_loss":round(float(log_loss(y,p,labels=[0,1])),5),
            "accuracy_50":round(float(np.mean((p>=0.5)==y)),5),
            "over_picks_at_65":int(picks.sum()),
            "over_hit_rate_at_65":round(float(y[picks].mean()),5) if picks.any() else None}

def _directional(y,p):
    y,p=np.asarray(y),np.asarray(p)
    take=np.maximum(p,1-p)>=0.65
    wins=np.where(p>=0.5,y==1,y==0)
    return {"picks_at_65_both_sides":int(take.sum()),
            "hit_rate_at_65_both_sides":round(float(wins[take].mean()),5) if take.any() else None}

def run_backtest(data, output_path):
    by_market=_build_rows(data)
    seasons=sorted(data["season"].unique())
    result={"data":{"seasons":seasons,"leagues":sorted(data["league"].unique()),"matches":int(len(data)),
        "first_date":str(data.date.min().date()),"last_date":str(data.date.max().date())},"markets":{}}
    logs=[]
    for market, rows in by_market.items():
        evaluated=[]
        by_fold={}
        for season in seasons:
            train=[r for r in rows if r["season"]<season]
            test=[r for r in rows if r["season"]==season]
            if len(train)<300 or not test: continue
            model=_model()
            model.fit(_feature_frame(train),[r["target"] for r in train])
            preds=model.predict_proba(_feature_frame(test))[:,1]
            fold_rows=[{**row,"candidate":float(p),"fold":season} for row,p in zip(test,preds)]
            evaluated.extend(fold_rows)
            by_fold[season]={"baseline_current_rule":_scores([r["target"] for r in test],[r["baseline"] for r in test]),
                             "candidate_regularized_logistic":_scores([r["target"] for r in test],preds)}
        if not evaluated:
            result["markets"][market]={"error":"Pas assez de saisons pour une évaluation walk-forward."}
            continue
        y=[r["target"] for r in evaluated]
        baseline=[r["baseline"] for r in evaluated]
        candidate=[r["candidate"] for r in evaluated]
        section={"test_seasons":sorted({r["fold"] for r in evaluated}),
                 "baseline_current_rule":_scores(y,baseline),
                 "candidate_regularized_logistic":_scores(y,candidate),
                 "by_season":by_fold}
        if market=="total_tirs":
            section["baseline_current_rule"].update(_directional(y,baseline))
            section["candidate_regularized_logistic"].update(_directional(y,candidate))
        result["markets"][market]=section
        for row in evaluated:
            logs.append({"market":market,"season":row["fold"],"date":row["date"],
                "target":row["target"],"baseline_p_over":row["baseline"],
                "candidate_p_over":row["candidate"]})
    result["note"]="Brier et log-loss évaluent les probabilités hors échantillon. Pas de ROI calculé: le CSV ne fournit pas les cotes des marchés tirs/cartons."
    path=Path(output_path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    pd.DataFrame(logs).to_csv(path.with_suffix(".csv"),index=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"Détails: {path.with_suffix('.csv')}")
    return result

def main():
    parser=argparse.ArgumentParser(description="Comparer règles actuelles et logistique par marché, en walk-forward")
    parser.add_argument("--seasons",nargs="*",default=["1718","1819","1920","2021","2122","2223","2324","2425"])
    parser.add_argument("--leagues",nargs="*",default=DEFAULT_LEAGUES)
    parser.add_argument("--output",default="data/sportapi_model_backtest.json")
    parser.add_argument("--export-model",default=None,help="Exporter les modèles finaux appris sur toutes les saisons")
    args=parser.parse_args()
    data=load_seasons(args.seasons,args.leagues)
    run_backtest(data,args.output)
    if args.export_model:
        export_models(data,args.export_model)

if __name__=="__main__": main()
