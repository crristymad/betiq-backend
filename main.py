from fastapi import FastAPI

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
