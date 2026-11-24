from fastapi import FastAPI
from sqlalchemy import text

from database import engine

app = FastAPI()

@app.get("/")
def root():
    return {
        "status": "online"
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

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }
from sqlalchemy import text
@app.get("/teams")
def teams():

    with engine.connect() as conn:

        rows = conn.execute(
            text("""
            SELECT id,name,short_name
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
