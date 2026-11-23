from fastapi import FastAPI
from sqlalchemy import text

from database import engine

app = FastAPI(
    title="BetIQ API",
    version="1.0.0"
)

@app.get("/")
def root():
    return {
        "status": "online",
        "app": "BetIQ API"
    }

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }

@app.get("/db")
def db():

    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT version();")
        ).scalar()

    return {
        "database": "connected",
        "version": version
    }

@app.get("/teams")
def teams():
    return {
        "message": "Teams endpoint coming soon"
    }

@app.get("/matches")
def matches():
    return {
        "message": "Matches endpoint coming soon"
    }

@app.get("/predictions")
def predictions():
    return {
        "message": "Predictions endpoint coming soon"
    }
