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
