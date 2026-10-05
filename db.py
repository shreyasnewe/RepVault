import os
from pymongo import MongoClient

# Connection string comes from the environment (Vercel Environment Variables)
MONGO_URI = os.environ.get("MONGODB_URI")

if not MONGO_URI:
    if os.environ.get("VERCEL"):
        # On Vercel, never fall back to localhost: it can't work there
        raise RuntimeError("MONGODB_URI is not set for this Vercel environment.")
    MONGO_URI = "mongodb://localhost:27017"   # local development only

DB_NAME = "repvault"

# Created once per process so connections are reused between requests
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, maxIdleTimeMS=5000)
db = client[DB_NAME]

users = db["users"]         # accounts
workouts = db["workouts"]   # one document per exercise per day (has user_id)
records = db["records"]     # all-time bests per user (has user_id)

# Indexes: usernames must be unique, and per-user lookups should be fast
try:
    users.create_index("username_lower", unique=True)
    workouts.create_index([("user_id", 1), ("workout_date", 1)])
except Exception:
    pass   # don't crash the app if the database is briefly unreachable


def check_connection():
    try:
        client.admin.command("ping")
        return True, ""
    except Exception as e:
        return False, str(e)