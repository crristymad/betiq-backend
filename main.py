from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def root():
    return {"status": "online"}

@app.get("/env")
def env():
    return {
        "database_url": os.getenv("DATABASE_URL", "")[:40]
    }
