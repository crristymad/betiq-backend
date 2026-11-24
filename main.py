import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests
from fastapi import FastAPI
from sqlalchemy import text

from database import engine

API_FOOTBALL_URL = "https://v3.football.api-sports.io"


def ensure_schema():
    """Adauga coloanele noi (doar daca nu exista deja)."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE teams ADD COLUMN IF NOT EXISTS api_id INTEGER"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS teams_api_id_idx ON teams(api_id)"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS api_id INTEGER"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS home_goals INTEGER"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS away_goals INTEGER"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS matches_api_id_idx ON matches(api_id)"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_schema()
    except Exception as e:
        print(f"ensure_schema a esuat: {e}")
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------
# Baza
# ---------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "online"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/db")
def db():
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version();")).scalar()
        return {"database": "connected", "version": version}
    except Exception as e:
        return {"error": str(e)}


@app.get("/db-schema")
def db_schema():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """)
        ).fetchall()
    return [{"table": r[0], "column": r[1], "type": r[2]} for r in rows]


@app.get("/teams")
def teams():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT id, name, short_name
            FROM teams
            ORDER BY name
            """)
        ).fetchall()
    return [{"id": r[0], "name": r[1], "short_name": r[2]} for r in rows]


@app.get("/matches")
def matches():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT
                m.id,
                home_t.name AS home_team,
                away_t.name AS away_team,
                m.kickoff,
                m.status,
                m.home_goals,
                m.away_goals
            FROM matches m
            JOIN teams home_t ON home_t.id = m.home_team_id
            JOIN teams away_t ON away_t.id = m.away_team_id
            ORDER BY m.kickoff
            """)
        ).fetchall()
    return [
        {
            "id": r[0],
            "home_team": r[1],
            "away_team": r[2],
            "kickoff": str(r[3]),
            "status": r[4],
            "home_goals": r[5],
            "away_goals": r[6],
        }
        for r in rows
    ]


def fetch_best_bets():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT
                home_t.name AS home_team,
                away_t.name AS away_team,
                br.recommended_odd,
                br.edge_percent,
                p.probability,
                p.confidence_score
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            JOIN teams home_t ON home_t.id = m.home_team_id
            JOIN teams away_t ON away_t.id = m.away_team_id
            JOIN bet_recommendations br ON br.prediction_id = p.id
            ORDER BY p.confidence_score DESC
            """)
        ).fetchall()
    return [
        {
            "match": f"{r[0]} vs {r[1]}",
            "odd": float(r[2]),
            "edge": float(r[3]),
            "probability": float(r[4]),
            "confidence": float(r[5]),
        }
        for r in rows
    ]


@app.get("/top-bets")
def top_bets():
    return fetch_best_bets()


@app.get("/dashboard")
def dashboard():
    bets = fetch_best_bets()
    return {"total_matches": len(bets), "best_bets": bets}


# ---------------------------------------------------------------
# Import din API-Football
# ---------------------------------------------------------------
@app.post("/import-league")
def import_league(league: int = 283, season: int = 2023):
    """Importa meciurile terminate (FT). Consuma 1 cerere API."""
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        return {"error": "API_FOOTBALL_KEY lipseste din Variables"}

    resp = requests.get(
        f"{API_FOOTBALL_URL}/fixtures",
        headers={"x-apisports-key": key},
        params={"league": league, "season": season, "status": "FT"},
        timeout=30,
    )
    data = resp.json()
    if data.get("errors"):
        return {"http_status": resp.status_code, "errors": data["errors"]}

    fixtures = data.get("response", [])
    team_cache = {}
    inserted = 0
    skipped = 0

    with engine.begin() as conn:

        def get_team_id(t):
            if t["id"] in team_cache:
                return team_cache[t["id"]]
            row = conn.execute(
                text("SELECT id FROM teams WHERE api_id = :a"), {"a": t["id"]}
            ).fetchone()
            if row:
                team_id = row[0]
            else:
                team_id = conn.execute(
                    text("""
                    INSERT INTO teams (name, short_name, api_id)
                    VALUES (:n, :s, :a)
                    RETURNING id
                    """),
                    {"n": t["name"], "s": t["name"][:3].upper(), "a": t["id"]},
                ).scalar()
            team_cache[t["id"]] = team_id
            return team_id

        for f in fixtures:
            if f["goals"]["home"] is None or f["goals"]["away"] is None:
                skipped += 1
                continue

            kickoff = (
                datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
            home_id = get_team_id(f["teams"]["home"])
            away_id = get_team_id(f["teams"]["away"])

            new_id = conn.execute(
                text("""
                INSERT INTO matches
                    (home_team_id, away_team_id, kickoff, status,
                     api_id, home_goals, away_goals)
                VALUES (:h, :a, :k, 'FT', :api, :hg, :ag)
                ON CONFLICT (api_id) DO NOTHING
                RETURNING id
                """),
                {
                    "h": home_id,
                    "a": away_id,
                    "k": kickoff,
                    "api": f["fixture"]["id"],
                    "hg": f["goals"]["home"],
                    "ag": f["goals"]["away"],
                },
            ).scalar()

            if new_id:
                inserted += 1
            else:
                skipped += 1

    return {
        "league": league,
        "season": season,
        "received": len(fixtures),
        "inserted": inserted,
        "skipped": skipped,
    }


# ---------------------------------------------------------------
# Model Poisson
# ---------------------------------------------------------------
def poisson_pmf(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_1x2(lam_h, lam_a, max_goals=10):
    p_home = p_draw = p_away = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a)
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def predict_from_history(history, home_id, away_id, min_games=3, shrink=0.0):
    """history = lista de (home_id, away_id, home_goals, away_goals)"""
    avg_h = sum(g[2] for g in history) / len(history)
    avg_a = sum(g[3] for g in history) / len(history)

    home_games = [g for g in history if g[0] == home_id]
    away_games = [g for g in history if g[1] == away_id]
    if len(home_games) < min_games or len(away_games) < min_games:
        return None
    n_h, n_a = len(home_games), len(away_games)

    home_attack = ((sum(g[2] for g in home_games) + shrink * avg_h) / (n_h + shrink)) / avg_h
    home_defense = ((sum(g[3] for g in home_games) + shrink * avg_a) / (n_h + shrink)) / avg_a
    away_attack = ((sum(g[3] for g in away_games) + shrink * avg_a) / (n_a + shrink)) / avg_a
    away_defense = ((sum(g[2] for g in away_games) + shrink * avg_h) / (n_a + shrink)) / avg_h

    lam_h = avg_h * home_attack * away_defense
    lam_a = avg_a * away_attack * home_defense
    return lam_h, lam_a, poisson_1x2(lam_h, lam_a)


def load_history():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT m.home_team_id, m.away_team_id, m.home_goals, m.away_goals,
                   home_t.name, away_t.name
            FROM matches m
            JOIN teams home_t ON home_t.id = m.home_team_id
            JOIN teams away_t ON away_t.id = m.away_team_id
            WHERE m.api_id IS NOT NULL AND m.home_goals IS NOT NULL
            ORDER BY m.kickoff, m.id
            """)
        ).fetchall()
    games = [(r[0], r[1], r[2], r[3]) for r in rows]
    names = [(r[4], r[5]) for r in rows]
    return games, names


@app.get("/predict")
def predict(home: str, away: str, shrink: float = 0.0):
    with engine.connect() as conn:

        def find_team(name):
            return conn.execute(
                text("""
                SELECT id, name FROM teams
                WHERE api_id IS NOT NULL AND name ILIKE :n
                ORDER BY name LIMIT 1
                """),
                {"n": f"%{name}%"},
            ).fetchone()

        h, a = find_team(home), find_team(away)

    if not h or not a:
        return {"error": "Echipa nu a fost gasita. Verifica numele in /teams"}

    games, _ = load_history()
    pred = predict_from_history(games, h[0], a[0], shrink=shrink)
    if pred is None:
        return {"error": "Prea putine meciuri istorice pentru una dintre echipe"}

    lam_h, lam_a, (p_home, p_draw, p_away) = pred
    return {
        "match": f"{h[1]} vs {a[1]}",
        "shrink": shrink,
        "expected_goals": {"home": round(lam_h, 2), "away": round(lam_a, 2)},
        "probability": {
            "home": round(p_home * 100, 1),
            "draw": round(p_draw * 100, 1),
            "away": round(p_away * 100, 1),
        },
        "fair_odds": {
            "home": round(1 / p_home, 2),
            "draw": round(1 / p_draw, 2),
            "away": round(1 / p_away, 2),
        },
    }


@app.get("/backtest-sample")
def backtest_sample(warmup: int = 80, n: int = 40, shrink: float = 0.0, details: bool = False):
    games, names = load_history()

    results = []
    skipped = 0
    b_model = b_naive = l_model = l_naive = 0.0
    evaluated = 0

    for i in range(warmup, min(warmup + n, len(games))):
        history = games[:i]
        hid, aid, hg, ag = games[i]

        pred = predict_from_history(history, hid, aid, shrink=shrink)
        if pred is None:
            skipped += 1
            continue
        _, _, model_p = pred

        naive_p = (
            sum(1 for g in history if g[2] > g[3]) / len(history),
            sum(1 for g in history if g[2] == g[3]) / len(history),
            sum(1 for g in history if g[2] < g[3]) / len(history),
        )

        actual = 0 if hg > ag else (1 if hg == ag else 2)
        outcome = [1 if k == actual else 0 for k in range(3)]

        b_model += sum((model_p[k] - outcome[k]) ** 2 for k in range(3))
        b_naive += sum((naive_p[k] - outcome[k]) ** 2 for k in range(3))
        l_model += -math.log(max(model_p[actual], 1e-9))
        l_naive += -math.log(max(naive_p[actual], 1e-9))
        evaluated += 1

        if details:
            results.append({
                "match": f"{names[i][0]} {hg}-{ag} {names[i][1]}",
                "model_1x2": [round(p * 100, 1) for p in model_p],
                "naive_1x2": [round(p * 100, 1) for p in naive_p],
                "actual": ["1", "X", "2"][actual],
            })

    if evaluated == 0:
        return {"error": "Nu s-au putut evalua meciuri", "skipped": skipped}

    out = {
        "evaluated": evaluated,
        "skipped": skipped,
        "shrink": shrink,
        "model": {"brier": round(b_model / evaluated, 4), "log_loss": round(l_model / evaluated, 4)},
        "naive": {"brier": round(b_naive / evaluated, 4), "log_loss": round(l_naive / evaluated, 4)},
        "reference_random_guess": {"brier": 0.6667, "log_loss": 1.0986},
        "model_better_than_naive": b_model < b_naive,
    }
    if details:
        out["matches"] = results
    return out


def _fd_num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _fd_int(v):
    n = _fd_num(v)
    return int(n) if n is not None else None


def _fd_date(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def parse_fd_rows(rows, league, season):
    """Transforma randurile CSV football-data.co.uk in dictionare pentru baza de date."""
    out = []
    for r in rows:
        d = _fd_date(r.get("Date"))
        hg, ag = _fd_int(r.get("FTHG")), _fd_int(r.get("FTAG"))
        home, away = r.get("HomeTeam"), r.get("AwayTeam")
        if d is None or hg is None or ag is None or not home or not away:
            continue
        out.append({
            "league": league,
            "season": season,
            "d": d,
            "home": home.strip(),
            "away": away.strip(),
            "hg": hg,
            "ag": ag,
            # cote inainte de meci (Bet365), cu varianta medie ca rezerva
            "oh": _fd_num(r.get("B365H")) or _fd_num(r.get("AvgH")),
            "od": _fd_num(r.get("B365D")) or _fd_num(r.get("AvgD")),
            "oa": _fd_num(r.get("B365A")) or _fd_num(r.get("AvgA")),
            # cote medii de inchidere
            "ch": _fd_num(r.get("AvgCH")),
            "cd": _fd_num(r.get("AvgCD")),
            "ca": _fd_num(r.get("AvgCA")),
            "hy": _fd_int(r.get("HY")),
            "ay": _fd_int(r.get("AY")),
            "hr": _fd_int(r.get("HR")),
            "ar": _fd_int(r.get("AR")),
        })
    return out


@app.post("/import-fd")
def import_fd(league: str = "E0", season: str = "2526"):
    """Importa un fisier football-data.co.uk (ex: E0, sezonul 2526 = 2025/26)."""
    import csv
    import io

    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "url": url}

    try:
        content = resp.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = resp.content.decode("latin-1")

    rows = list(csv.DictReader(io.StringIO(content)))
    parsed = parse_fd_rows(rows, league, season)

    inserted = 0
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS hist_matches (
                id SERIAL PRIMARY KEY,
                league TEXT NOT NULL,
                season TEXT NOT NULL,
                match_date DATE NOT NULL,
                home TEXT NOT NULL,
                away TEXT NOT NULL,
                hg INTEGER NOT NULL,
                ag INTEGER NOT NULL,
                odd_h NUMERIC, odd_d NUMERIC, odd_a NUMERIC,
                close_h NUMERIC, close_d NUMERIC, close_a NUMERIC,
                hy INTEGER, ay INTEGER, hr INTEGER, ar INTEGER,
                UNIQUE (league, season, match_date, home, away)
            )
        """))
        for p in parsed:
            res = conn.execute(
                text("""
                INSERT INTO hist_matches
                    (league, season, match_date, home, away, hg, ag,
                     odd_h, odd_d, odd_a, close_h, close_d, close_a,
                     hy, ay, hr, ar)
                VALUES
                    (:league, :season, :d, :home, :away, :hg, :ag,
                     :oh, :od, :oa, :ch, :cd, :ca,
                     :hy, :ay, :hr, :ar)
                ON CONFLICT (league, season, match_date, home, away) DO NOTHING
                """),
                p,
            )
            inserted += res.rowcount

    return {
        "url": url,
        "rows_in_file": len(rows),
        "parsed": len(parsed),
        "inserted": inserted,
        "with_odds": sum(1 for p in parsed if p["oh"] and p["od"] and p["oa"]),
    }


@app.get("/hist-summary")
def hist_summary():
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT league, season, COUNT(*), MIN(match_date), MAX(match_date),
                   COUNT(odd_h)
            FROM hist_matches
            GROUP BY league, season
            ORDER BY league, season
        """)).fetchall()
    return [
        {
            "league": r[0], "season": r[1], "matches": r[2],
            "from": str(r[3]), "to": str(r[4]), "with_odds": r[5],
        }
        for r in rows
    ]


@app.post("/import-fd-all")
def import_fd_all(
    leagues: str = "E0,SC0,D1",
    seasons: str = "2526,2425,2324,2223,2122,2021",
    pause: float = 1.0,
):
    """Importa mai multe ligi/sezoane football-data.co.uk, cate un fisier pe rand."""
    import time

    league_list = [x.strip() for x in leagues.split(",") if x.strip()]
    season_list = [x.strip() for x in seasons.split(",") if x.strip()]

    total = 0
    files = 0
    failed = []

    for lg in league_list:
        for se in season_list:
            files += 1
            try:
                r = import_fd(league=lg, season=se)
            except Exception as e:
                r = {"error": str(e)[:200]}

            if r.get("error"):
                failed.append({"league": lg, "season": se, "error": r["error"]})
            else:
                total += r.get("inserted", 0)
            time.sleep(pause)

    return {"files_tried": files, "total_inserted": total, "failed": failed}


@app.get("/backtest-sweep")
def backtest_sweep(warmup: int = 80, n: int = 240, shrinks: str = "0,5,10,20,40"):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT m.home_team_id, m.away_team_id, m.home_goals, m.away_goals
            FROM matches m
            WHERE m.api_id IS NOT NULL AND m.home_goals IS NOT NULL
            ORDER BY m.kickoff, m.id
            """)
        ).fetchall()

    games = [(r[0], r[1], r[2], r[3]) for r in rows]
    values = [float(s) for s in shrinks.split(",") if s.strip()]

    table = []
    for shrink in values:
        b_model = b_naive = l_model = l_naive = 0.0
        evaluated = 0

        for i in range(warmup, min(warmup + n, len(games))):
            history = games[:i]
            hid, aid, hg, ag = games[i]

            pred = predict_from_history(history, hid, aid, shrink=shrink)
            if pred is None:
                continue
            model_p = pred[2]

            naive_p = (
                sum(1 for g in history if g[2] > g[3]) / len(history),
                sum(1 for g in history if g[2] == g[3]) / len(history),
                sum(1 for g in history if g[2] < g[3]) / len(history),
            )

            actual = 0 if hg > ag else (1 if hg == ag else 2)
            outcome = [1 if k == actual else 0 for k in range(3)]

            b_model += sum((model_p[k] - outcome[k]) ** 2 for k in range(3))
            b_naive += sum((naive_p[k] - outcome[k]) ** 2 for k in range(3))
            l_model += -math.log(max(model_p[actual], 1e-9))
            l_naive += -math.log(max(naive_p[actual], 1e-9))
            evaluated += 1

        if evaluated == 0:
            table.append({"shrink": shrink, "error": "nu s-au evaluat meciuri"})
            continue

        table.append({
            "shrink": shrink,
            "evaluated": evaluated,
            "model_brier": round(b_model / evaluated, 4),
            "naive_brier": round(b_naive / evaluated, 4),
            "model_logloss": round(l_model / evaluated, 4),
            "naive_logloss": round(l_naive / evaluated, 4),
            "model_better_than_naive": b_model < b_naive,
        })

    return {"results": table}


@app.get("/value-check")
def value_check(
    home: str,
    away: str,
    odd_home: float,
    odd_draw: float,
    odd_away: float,
    shrink: float = 10.0,
    min_edge: float = 0.05,
):
    with engine.connect() as conn:

        def find_team(name):
            return conn.execute(
                text("""
                SELECT id, name FROM teams
                WHERE api_id IS NOT NULL AND name ILIKE :n
                ORDER BY name LIMIT 1
                """),
                {"n": f"%{name}%"},
            ).fetchone()

        h, a = find_team(home), find_team(away)
        if not h or not a:
            return {"error": "Echipa nu a fost gasita. Verifica numele in /teams"}

        rows = conn.execute(
            text("""
            SELECT home_team_id, away_team_id, home_goals, away_goals
            FROM matches
            WHERE api_id IS NOT NULL AND home_goals IS NOT NULL
            ORDER BY kickoff, id
            """)
        ).fetchall()

    games = [(r[0], r[1], r[2], r[3]) for r in rows]
    pred = predict_from_history(games, h[0], a[0], shrink=shrink)
    if pred is None:
        return {"error": "Prea putine meciuri istorice pentru una dintre echipe"}

    lam_h, lam_a, probs = pred
    odds = [odd_home, odd_draw, odd_away]
    labels = ["home", "draw", "away"]

    # probabilitatea implicita a pietei, fara marja casei
    inv = [1 / o for o in odds]
    overround = sum(inv)
    market = [x / overround for x in inv]

    outcomes = []
    best = None
    for k in range(3):
        edge = probs[k] * odds[k] - 1
        item = {
            "outcome": labels[k],
            "odd": odds[k],
            "model_prob_pct": round(probs[k] * 100, 1),
            "market_prob_pct": round(market[k] * 100, 1),
            "fair_odd": round(1 / probs[k], 2),
            "edge_pct": round(edge * 100, 1),
        }
        outcomes.append(item)
        if best is None or edge > best[0]:
            best = (edge, labels[k])

    best_edge, best_label = best
    if best_edge >= 0.15:
        verdict = f"Edge foarte mare pe {best_label}: suspect, probabil greseste modelul. Nu paria pe baza asta."
    elif best_edge >= min_edge:
        verdict = f"Edge teoretic pozitiv pe {best_label}, dar avantajul modelului e mic. Interpreteaza cu prudenta."
    else:
        verdict = "Nu paria: nicio cota nu depaseste pragul de edge."

    return {
        "match": f"{h[1]} vs {a[1]}",
        "shrink": shrink,
        "bookmaker_margin_pct": round((overround - 1) * 100, 1),
        "outcomes": outcomes,
        "verdict": verdict,
        "warning": "Model antrenat pe date 2021-2023, cu avantaj mic fata de frecventa simpla a rezultatelor. Nu e sfat financiar.",
    }


@app.get("/backtest-odds")
def backtest_odds(
    leagues: str = "E0",
    warmup: int = 300,
    shrink: float = 10.0,
    lo: float = 1.4,
    hi: float = 1.7,
    min_edge: float = 0.0,
    use_close: bool = False,
):
    """Simuleaza pariuri de 1 unitate pe victoria gazdei/oaspetelui, cu cota intre lo si hi.

    baseline = pariaza pe orice favorit cu cota in interval (fara model)
    model    = alege echipa cu probabilitatea cea mai mare dupa model,
               doar daca edge = p * cota - 1 >= min_edge
    """
    league_list = [x.strip() for x in leagues.split(",") if x.strip()]

    def summarize(profits):
        n = len(profits)
        if n == 0:
            return {"bets": 0}
        wins = sum(1 for p in profits if p > 0)
        total = sum(profits)
        mean = total / n
        var = sum((p - mean) ** 2 for p in profits) / n
        se = (var / n) ** 0.5
        return {
            "bets": n,
            "wins": wins,
            "hit_rate_pct": round(100 * wins / n, 1),
            "profit_units": round(total, 2),
            "roi_pct": round(100 * mean, 2),
            "roi_se_pct": round(100 * se, 2),
        }

    by_league = []
    all_model, all_base = [], []
    season_stats = {}

    with engine.connect() as conn:
        for lg in league_list:
            rows = conn.execute(
                text("""
                SELECT home, away, hg, ag,
                       odd_h, odd_a, close_h, close_a, season
                FROM hist_matches
                WHERE league = :l
                ORDER BY match_date, id
                """),
                {"l": lg},
            ).fetchall()

            games = [(r[0], r[1], r[2], r[3]) for r in rows]
            base, model = [], []
            evaluated = 0

            for i in range(warmup, len(rows)):
                r = rows[i]
                if use_close and r[6] and r[7]:
                    oh, oa = float(r[6]), float(r[7])
                elif r[4] and r[5]:
                    oh, oa = float(r[4]), float(r[5])
                else:
                    continue
                evaluated += 1

                cands = []
                if lo <= oh <= hi:
                    cands.append(("H", oh))
                if lo <= oa <= hi:
                    cands.append(("A", oa))
                if not cands:
                    continue

                hg, ag = r[2], r[3]

                def result(side, odd):
                    won = hg > ag if side == "H" else ag > hg
                    return (odd - 1) if won else -1.0

                # baseline: favoritul cu cota cea mai mica din interval
                side_b, odd_b = min(cands, key=lambda c: c[1])
                pb = result(side_b, odd_b)
                base.append(pb)
                st = season_stats.setdefault(r[8], {"base": [], "model": []})
                st["base"].append(pb)

                pred = predict_from_history(games[:i], r[0], r[1], shrink=shrink)
                if pred is None:
                    continue
                p_home, _, p_away = pred[2]
                options = [
                    (side, odd, p_home if side == "H" else p_away)
                    for side, odd in cands
                ]
                side_m, odd_m, p_m = max(options, key=lambda o: o[2])
                if p_m * odd_m - 1 >= min_edge:
                    pm = result(side_m, odd_m)
                    model.append(pm)
                    st["model"].append(pm)

            all_base += base
            all_model += model
            by_league.append({
                "league": lg,
                "matches_evaluated": evaluated,
                "baseline": summarize(base),
                "model": summarize(model),
            })

    return {
        "params": {
            "warmup": warmup, "shrink": shrink, "odds_range": [lo, hi],
            "min_edge": min_edge, "odds_used": "close" if use_close else "pre-match (Bet365)",
        },
        "total": {"baseline": summarize(all_base), "model": summarize(all_model)},
        "by_league": by_league,
        "by_season": [
            {
                "season": s,
                "baseline": summarize(v["base"]),
                "model": summarize(v["model"]),
            }
            for s, v in sorted(season_stats.items())
        ],
        "note": "roi_se_pct = eroarea standard; daca |roi_pct| < 2 * roi_se_pct, rezultatul nu e diferit de zgomot.",
    }


@app.get("/team-profile")
def team_profile(league: str, team: str, n: int = 8):
    """Forma, split acasa/deplasare si cartonase pentru o echipa, din hist_matches."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
            SELECT home FROM hist_matches
            WHERE league = :l AND home ILIKE :t
            GROUP BY home ORDER BY COUNT(*) DESC LIMIT 1
            """),
            {"l": league, "t": f"%{team}%"},
        ).fetchone()
        if not row:
            return {"error": "Echipa nu a fost gasita in aceasta liga"}
        name = row[0]

        rows = conn.execute(
            text("""
            SELECT match_date, home, away, hg, ag, hy, ay, hr, ar
            FROM hist_matches
            WHERE league = :l AND (home = :n OR away = :n)
            ORDER BY match_date DESC, id DESC
            LIMIT 40
            """),
            {"l": league, "n": name},
        ).fetchall()

    games = []
    for r in rows:
        is_home = r[1] == name
        gf, ga = (r[3], r[4]) if is_home else (r[4], r[3])
        yc, rc = (r[5], r[7]) if is_home else (r[6], r[8])
        res = "W" if gf > ga else ("D" if gf == ga else "L")
        games.append({
            "date": str(r[0]),
            "venue": "H" if is_home else "A",
            "opponent": r[2] if is_home else r[1],
            "score": f"{gf}-{ga}",
            "result": res,
            "yellow": yc,
            "red": rc,
            "_gf": gf, "_ga": ga,
        })

    def summarize(g):
        if not g:
            return {"matches": 0}
        pts = sum(3 if x["result"] == "W" else 1 if x["result"] == "D" else 0 for x in g)
        yc = [x["yellow"] for x in g if x["yellow"] is not None]
        rc = [x["red"] for x in g if x["red"] is not None]
        return {
            "matches": len(g),
            "form": "".join(x["result"] for x in g),
            "points": pts,
            "points_per_match": round(pts / len(g), 2),
            "goals_for": sum(x["_gf"] for x in g),
            "goals_against": sum(x["_ga"] for x in g),
            "avg_yellow": round(sum(yc) / len(yc), 2) if yc else None,
            "avg_red": round(sum(rc) / len(rc), 2) if rc else None,
        }

    last = games[:n]
    home_g = [x for x in games if x["venue"] == "H"][:5]
    away_g = [x for x in games if x["venue"] == "A"][:5]

    return {
        "team": name,
        "league": league,
        "last_n": summarize(last),
        "last_5_home": summarize(home_g),
        "last_5_away": summarize(away_g),
        "matches": [{k: v for k, v in x.items() if not k.startswith("_")} for x in last],
    }


FORM_CHART_HTML = """<!doctype html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BetIQ - Forma echipei</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 24px auto; padding: 0 12px; color: #1a1a1a; }
  form { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  input, button { padding: 8px 10px; font-size: 14px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 16px 0; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 10px 12px; }
  .card h3 { margin: 0 0 6px; font-size: 14px; color: #555; }
  .big { font-size: 20px; font-weight: 600; letter-spacing: 2px; }
  .err { color: #b00020; }
  .chart-box { position: relative; height: 320px; }
</style>
</head>
<body>
<h2 id="title">Forma echipei</h2>
<form id="f">
  <input name="league" placeholder="liga (ex: E0)" value="E0">
  <input name="team" placeholder="echipa (ex: Liverpool)" value="Liverpool">
  <input name="n" type="number" min="3" max="20" value="8" style="width:70px">
  <button type="submit">Arata</button>
</form>
<div id="msg" class="err"></div>
<div class="cards" id="cards"></div>
<div class="chart-box"><canvas id="chart"></canvas></div>
<p style="font-size:12px;color:#666">Puncte pe meci: victorie 3, egal 1, infrangere 0. Date: hist_matches. Informativ, nu recomandare de pariuri.</p>
<script>
var chart = null;
var PTS = { W: 3, D: 1, L: 0 };
var COLOR = { W: "#2e9e5b", D: "#e0a800", L: "#d64545" };

function card(title, s) {
  var d = document.createElement("div");
  d.className = "card";
  var h = document.createElement("h3");
  h.textContent = title;
  d.appendChild(h);
  var big = document.createElement("div");
  big.className = "big";
  big.textContent = s.matches ? s.form : "-";
  d.appendChild(big);
  var p = document.createElement("div");
  p.textContent = s.matches
    ? s.points_per_match + " p/meci | goluri " + s.goals_for + "-" + s.goals_against +
      " | galbene " + s.avg_yellow + " | rosii " + s.avg_red
    : "fara date";
  d.appendChild(p);
  return d;
}

async function load(params) {
  var msg = document.getElementById("msg");
  msg.textContent = "";
  var url = "/team-profile?league=" + encodeURIComponent(params.league) +
            "&team=" + encodeURIComponent(params.team) + "&n=" + encodeURIComponent(params.n);
  var data;
  try {
    var r = await fetch(url);
    data = await r.json();
  } catch (e) {
    msg.textContent = "Eroare la incarcarea datelor.";
    return;
  }
  if (data.error) { msg.textContent = data.error; return; }

  document.getElementById("title").textContent = "Forma: " + data.team + " (" + data.league + ")";
  var cards = document.getElementById("cards");
  cards.textContent = "";
  cards.appendChild(card("Ultimele " + params.n + " meciuri", data.last_n));
  cards.appendChild(card("Ultimele 5 acasa", data.last_5_home));
  cards.appendChild(card("Ultimele 5 in deplasare", data.last_5_away));

  var ms = data.matches.slice().reverse();
  var labels = ms.map(function (m) { return m.date + " " + m.venue + " " + m.opponent + " " + m.score; });
  var values = ms.map(function (m) { return PTS[m.result]; });
  var colors = ms.map(function (m) { return COLOR[m.result]; });

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("chart"), {
    type: "bar",
    data: { labels: labels, datasets: [{ label: "Puncte", data: values, backgroundColor: colors }] },
    options: {
      maintainAspectRatio: false,
      scales: { y: { min: 0, max: 3, ticks: { stepSize: 1 } }, x: { ticks: { maxRotation: 60, minRotation: 40 } } },
      plugins: { legend: { display: false } }
    }
  });
}

function paramsFromUrl() {
  var q = new URLSearchParams(location.search);
  return { league: q.get("league") || "E0", team: q.get("team") || "Liverpool", n: q.get("n") || "8" };
}

document.getElementById("f").addEventListener("submit", function (e) {
  e.preventDefault();
  var fd = new FormData(e.target);
  var p = { league: fd.get("league"), team: fd.get("team"), n: fd.get("n") };
  history.replaceState(null, "", "?league=" + encodeURIComponent(p.league) +
    "&team=" + encodeURIComponent(p.team) + "&n=" + encodeURIComponent(p.n));
  load(p);
});

var init = paramsFromUrl();
document.querySelector("[name=league]").value = init.league;
document.querySelector("[name=team]").value = init.team;
document.querySelector("[name=n]").value = init.n;
load(init);
</script>
</body>
</html>
"""


@app.get("/form-chart")
def form_chart():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(FORM_CHART_HTML)


@app.get("/match-card")
def match_card(
    league: str,
    home: str,
    away: str,
    odd_home: float,
    odd_draw: float = 0.0,
    odd_away: float = 0.0,
    pick: str = "H",
    shrink: float = 10.0,
):
    """Card de analiza pentru un meci (liga = cod football-data, ex: E0).

    pick: H = victorie gazde, D = egal, A = victorie oaspeti.
    Numele echipelor sunt cele din football-data (ex: 'Man City', 'Tottenham').
    """
    pick = pick.upper()
    if pick not in ("H", "D", "A"):
        return {"error": "pick trebuie sa fie H, D sau A"}

    with engine.connect() as conn:

        def canon(name):
            r = conn.execute(
                text("""
                SELECT t FROM (
                    SELECT home AS t FROM hist_matches WHERE league = :l
                    UNION ALL
                    SELECT away AS t FROM hist_matches WHERE league = :l
                ) x
                WHERE t ILIKE :n
                GROUP BY t ORDER BY COUNT(*) DESC LIMIT 1
                """),
                {"l": league, "n": f"%{name}%"},
            ).fetchone()
            return r[0] if r else None

        h, a = canon(home), canon(away)
        if not h or not a:
            names = conn.execute(
                text("SELECT DISTINCT home FROM hist_matches WHERE league = :l ORDER BY home"),
                {"l": league},
            ).fetchall()
            return {
                "error": "Echipa nu a fost gasita. Foloseste numele din football-data.",
                "teams_in_league": [x[0] for x in names],
            }

        rows = conn.execute(
            text("""
            SELECT home, away, hg, ag, match_date
            FROM hist_matches
            WHERE league = :l
            ORDER BY match_date, id
            """),
            {"l": league},
        ).fetchall()

    games = [(r[0], r[1], r[2], r[3]) for r in rows]
    pred = predict_from_history(games, h, a, shrink=shrink)
    if pred is None:
        return {"error": "Prea putine meciuri istorice pentru una dintre echipe"}

    _, _, (p_h, p_d, p_a) = pred
    probs = {"H": p_h, "D": p_d, "A": p_a}
    odds = {"H": odd_home, "D": odd_draw, "A": odd_away}
    odd = odds[pick]
    if odd <= 1:
        return {"error": "Cota pentru selectia aleasa trebuie sa fie mai mare ca 1"}

    if odd_home > 1 and odd_draw > 1 and odd_away > 1:
        inv = [1 / odd_home, 1 / odd_draw, 1 / odd_away]
        market = (1 / odd) / sum(inv)
        market_note = "fara marja casei"
    else:
        market = 1 / odd
        market_note = "implicita, include marja casei (da toate cele 3 cote pentru valoarea corecta)"

    p = probs[pick]
    edge = p * odd - 1
    gap = abs(p - market)

    def form(team):
        g = [x for x in games if x[0] == team or x[1] == team][-5:]
        pts = 0
        for hh, aa, hg, ag in g:
            gf, ga = (hg, ag) if hh == team else (ag, hg)
            pts += 3 if gf > ga else (1 if gf == ga else 0)
        return len(g), pts

    nh, pts_h = form(h)
    na, pts_a = form(a)

    def count(team):
        return sum(1 for x in games if x[0] == team or x[1] == team)

    n_min = min(count(h), count(a))
    sample_pts = 40 * min(1.0, n_min / 100)
    agree_pts = 40 * max(0.0, 1 - gap / 0.15)
    recent_pts = 20 if (nh == 5 and na == 5) else 0
    confidence = round(sample_pts + agree_pts + recent_pts)

    if edge >= 0.15:
        label = "Diferenta foarte mare fata de piata: de obicei greseste modelul, nu e oportunitate"
    elif edge >= 0.03:
        label = "Edge teoretic mic, nedemonstrat in backtest"
    else:
        label = "Fara avantaj fata de piata"

    return {
        "match": f"{h} vs {a}",
        "league": league,
        "selection": {"H": f"Victorie {h}", "D": "Egal", "A": f"Victorie {a}"}[pick],
        "odd": odd,
        "model_prob_pct": round(p * 100, 1),
        "market_prob_pct": round(market * 100, 1),
        "market_note": market_note,
        "fair_odd": round(1 / p, 2),
        "edge_pct": round(edge * 100, 1),
        "form_10": {
            h: round(pts_h / 15 * 10, 1) if nh == 5 else None,
            a: round(pts_a / 15 * 10, 1) if na == 5 else None,
        },
        "analysis_confidence_0_100": confidence,
        "confidence_parts": {
            "sample_size_max_40": round(sample_pts),
            "model_vs_market_max_40": round(agree_pts),
            "recent_matches_max_20": recent_pts,
        },
        "assessment": label,
        "missing_inputs": ["lot disponibil", "accidentari", "suspendari", "motivatie"],
        "warning": "Backtest-ul pe cote de inchidere nu a aratat avantaj fata de piata. Informativ, nu recomandare de pariuri.",
    }
