import os

DATABASE_URL = os.getenv("DATABASE_URL")

print("DATABASE_URL =", DATABASE_URL)

# comentăm temporar SQLAlchemy
# from sqlalchemy import create_engine
# from sqlalchemy.orm import sessionmaker
# engine = create_engine(DATABASE_URL)
