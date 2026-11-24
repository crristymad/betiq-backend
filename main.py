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
