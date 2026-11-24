from fastapi import FastAPI
from sqlalchemy import text

from database import engine

app = FastAPI()


@app.get("/")
def root():
    return {
        "status": "online"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/db")
def db():
    try:

        with engine.connect() as conn:

            version = conn.execute(
                text("SELECT version();")
            ).scalar()

        return {
            "database": "connected",
            "version": version
        }

    except Exception as e:

        return {
            "error": str(e)
        }


@app.get("/teams")
def teams():

    with engine.connect() as conn:

        rows = conn.execute(
            text("""
            SELECT
                id,
                name,
                short_name
            FROM teams
            ORDER BY name
            """)
        ).fetchall()

    return [
        {
            "id": r[0],
            "name": r[1],
            "short_name": r[2]
        }
        for r in rows
    ]


@app.get("/matches")
def matches():

    with engine.connect() as conn:

        rows = conn.execute(
            text("""
            SELECT
                m.id,
                ht.name as home_team,
                at.name as away_team,
                m.kickoff,
                m.status
            FROM matches m
            JOIN teams ht
                ON ht.id = m.home_team_id
            JOIN teams at
                ON at.id = m.away_team_id
            ORDER BY m.kickoff
            """)
        ).fetchall()

    return [
        {
            "id": r[0],
            "home_team": r[1],
            "away_team": r[2],
            "kickoff": str(r[3]),
            "status": r[4]
        }
        for r in rows
    ]
@app.get("/top-bets")
def top_bets():

    with engine.connect() as conn:

        rows = conn.execute(
            text("""
            SELECT
                ht.name as home_team,
                at.name as away_team,

                br.recommended_odd,
                br.edge_percent,

                p.probability,
                p.confidence_score

            FROM predictions p

            JOIN matches m
                ON m.id = p.match_id

            JOIN teams ht
                ON ht.id = m.home_team_id

            JOIN teams at
                ON at.id = m.away_team_id

            JOIN bet_recommendations br
                ON br.prediction_id = p.id

            ORDER BY p.confidence_score DESC
            """)
        ).fetchall()

    return [
        {
            "match": f"{r[0]} vs {r[1]}",
            "odd": float(r[2]),
            "edge": float(r[3]),
            "probability": float(r[4]),
            "confidence": float(r[5])
        }
        for r in rows
    ]
@app.get("/dashboard")
def dashboard():

    with engine.connect() as conn:

        rows = conn.execute(
            text("""
            SELECT
                ht.name as home_team,
                at.name as away_team,

                br.recommended_odd,
                br.edge_percent,

                p.probability,
                p.confidence_score

            FROM predictions p

            JOIN matches m
                ON m.id = p.match_id

            JOIN teams ht
                ON ht.id = m.home_team_id

            JOIN teams at
                ON at.id = m.away_team_id

            JOIN bet_recommendations br
                ON br.prediction_id = p.id

            ORDER BY p.confidence_score DESC
            """)
        ).fetchall()

    return {
        "total_matches": len(rows),
        "best_bets": [
            {
                "match": f"{r[0]} vs {r[1]}",
                "odd": float(r[2]),
                "edge": float(r[3]),
                "probability": float(r[4]),
                "confidence": float(r[5])
            }
            for r in rows
        ]
    }
import os
import requests

@app.get("/test-api-football")
def test_api_football():
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        return {"error": "API_FOOTBALL_KEY lipseste din Variables"}

    resp = requests.get(
        "https://v3.football.api-sports.io/fixtures",
        headers={"x-apisports-key": key},
        params={"league": 283, "season": 2023, "status": "FT"},
        timeout=15,
    )
    data = resp.json()

    return {
        "http_status": resp.status_code,
        "errors": data.get("errors"),
        "results": data.get("results"),
        "sample": [
            f"{f['teams']['home']['name']} {f['goals']['home']}-{f['goals']['away']} {f['teams']['away']['name']}"
            for f in data.get("response", [])[:3]
        ],
    }
@app.get("/import-one")
def import_one():
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        return {"error": "API_FOOTBALL_KEY lipseste"}

    resp = requests.get(
        "https://v3.football.api-sports.io/fixtures",
        headers={"x-apisports-key": key},
        params={"league": 283, "season": 2023, "status": "FT"},
        timeout=15,
    )
    f = resp.json()["response"][0]

    with engine.begin() as conn:
        # coloane noi (se adauga doar daca nu exista)
        conn.execute(text("ALTER TABLE teams ADD COLUMN IF NOT EXISTS api_id INTEGER"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS teams_api_id_idx ON teams(api_id)"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS api_id INTEGER"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS home_goals INTEGER"))
        conn.execute(text("ALTER TABLE matches ADD COLUMN IF NOT EXISTS away_goals INTEGER"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS matches_api_id_idx ON matches(api_id)"))

        def get_team_id(t):
            row = conn.execute(
                text("SELECT id FROM teams WHERE api_id = :a"), {"a": t["id"]}
            ).fetchone()
            if row:
                return row[0]
            return conn.execute(
                text("""INSERT INTO teams (name, short_name, api_id)
                        VALUES (:n, :s, :a) RETURNING id"""),
                {"n": t["name"], "s": t["name"][:3].upper(), "a": t["id"]},
            ).scalar()

        home_id = get_team_id(f["teams"]["home"])
        away_id = get_team_id(f["teams"]["away"])

        match_id = conn.execute(
            text("""INSERT INTO matches
                    (home_team_id, away_team_id, kickoff, status,
                     api_id, home_goals, away_goals)
                    VALUES (:h, :a, :k, 'FT', :api, :hg, :ag)
                    ON CONFLICT (api_id) DO NOTHING
                    RETURNING id"""),
            {
                "h": home_id, "a": away_id,
                "k": f["fixture"]["date"],
                "api": f["fixture"]["id"],
                "hg": f["goals"]["home"], "ag": f["goals"]["away"],
            },
        ).scalar()

    return {
        "saved_match_id": match_id,  # None = exista deja
        "match": f"{f['teams']['home']['name']} {f['goals']['home']}-{f['goals']['away']} {f['teams']['away']['name']}",
    }
