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
