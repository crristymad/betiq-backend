from fastapi import FastAPI
from sqlalchemy import text
from database import engine

app = FastAPI()

@app.get("/")
def root():
    return {"status": "online"}

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
