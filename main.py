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


@app.post("/import-league")
def import_league(league: int = 283, season: int = 2023):
    """Importa meciurile terminate (FT) dintr-o liga/sezon. Consuma 1 cerere API."""
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
def poisson_pmf(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)


@app.get("/predict")
def predict(home: str, away: str):
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

        lg = conn.execute(
            text("""
            SELECT AVG(home_goals), AVG(away_goals) FROM matches
            WHERE home_goals IS NOT NULL
            """)
        ).fetchone()
        avg_h, avg_a = float(lg[0]), float(lg[1])

        # gazda: goluri marcate / primite acasa
        hs = conn.execute(
            text("""
            SELECT AVG(home_goals), AVG(away_goals), COUNT(*) FROM matches
            WHERE home_team_id = :t AND home_goals IS NOT NULL
            """),
            {"t": h[0]},
        ).fetchone()

        # oaspete: goluri marcate / primite in deplasare
        aw = conn.execute(
            text("""
            SELECT AVG(away_goals), AVG(home_goals), COUNT(*) FROM matches
            WHERE away_team_id = :t AND home_goals IS NOT NULL
            """),
            {"t": a[0]},
        ).fetchone()

    if not hs[2] or not aw[2]:
        return {"error": "Nu exista meciuri istorice pentru una dintre echipe"}

    home_attack = float(hs[0]) / avg_h
    home_defense = float(hs[1]) / avg_a
    away_attack = float(aw[0]) / avg_a
    away_defense = float(aw[1]) / avg_h

    lam_h = avg_h * home_attack * away_defense
    lam_a = avg_a * away_attack * home_defense

    p_home = p_draw = p_away = 0.0
    for i in range(11):
        for j in range(11):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a)
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p

    total = p_home + p_draw + p_away
    p_home, p_draw, p_away = p_home / total, p_draw / total, p_away / total

    return {
        "match": f"{h[1]} vs {a[1]}",
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
        "sample_games": {"home_team_home": hs[2], "away_team_away": aw[2]},
    }
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


@app.get("/backtest-sample")
def backtest_sample(warmup: int = 80, n: int = 40, shrink: float = 0.0):
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

    results = []
    skipped = 0
    b_model = b_naive = l_model = l_naive = 0.0

    for i in range(warmup, min(warmup + n, len(games))):
        history = games[:i]
        hid, aid, hg, ag = games[i]

        pred = predict_from_history(history, hid, aid, shrink=shrink)
        if pred is None:
            skipped += 1
            continue
        lam_h, lam_a, model_p = pred

        # varianta naiva: frecventa rezultatelor de pana atunci
        naive_p = (
            sum(1 for g in history if g[2] > g[3]) / len(history),
            sum(1 for g in history if g[2] == g[3]) / len(history),
            sum(1 for g in history if g[2] < g[3]) / len(history),
        )

        actual = 0 if hg > ag else (1 if hg == ag else 2)
        outcome = [1 if k == actual else 0 for k in range(3)]

        b_m = sum((model_p[k] - outcome[k]) ** 2 for k in range(3))
        b_n = sum((naive_p[k] - outcome[k]) ** 2 for k in range(3))
        b_model += b_m
        b_naive += b_n
        l_model += -math.log(max(model_p[actual], 1e-9))
        l_naive += -math.log(max(naive_p[actual], 1e-9))

        results.append({
            "match": f"{names[i][0]} {hg}-{ag} {names[i][1]}",
            "model_1x2": [round(p * 100, 1) for p in model_p],
            "naive_1x2": [round(p * 100, 1) for p in naive_p],
            "actual": ["1", "X", "2"][actual],
        })

    k = len(results)
    if k == 0:
        return {"error": "Nu s-au putut evalua meciuri", "skipped": skipped}

    return {
        "evaluated": k,
        "skipped": skipped,
        "model": {"brier": round(b_model / k, 4), "log_loss": round(l_model / k, 4)},
        "naive": {"brier": round(b_naive / k, 4), "log_loss": round(l_naive / k, 4)},
        "reference_random_guess": {"brier": 0.6667, "log_loss": 1.0986},
        "model_better_than_naive": b_model < b_naive,
        "matches": results,
    }
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

    with engine.connect() as conn:
        for lg in league_list:
            rows = conn.execute(
                text("""
                SELECT home, away, hg, ag,
                       odd_h, odd_a, close_h, close_a
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
                base.append(result(side_b, odd_b))

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
                    model.append(result(side_m, odd_m))

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
        "note": "roi_se_pct = eroarea standard; daca |roi_pct| < 2 * roi_se_pct, rezultatul nu e diferit de zgomot.",
    }
