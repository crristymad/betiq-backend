@app.get("/env")
def env():
    return {
        "database_url": os.getenv("DATABASE_URL", "")[:20]
    }
