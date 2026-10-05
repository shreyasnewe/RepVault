import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = "repvault"

# Created once per process so connections are reused between requests
client = MongoClient("mongodb+srv://Vercel-Admin-repvault:rKqv7orIWCkTMEfa@repvault.ni5wdcj.mongodb.net/?retryWrites=true&w=majority", serverSelectionTimeoutMS=5000, maxIdleTimeMS=5000)
db = client[DB_NAME]

workouts = db["workouts"]   # one document per exercise per day
records = db["records"]     # all-time bests, never pruned


def check_connection():
    try:
        client.admin.command("ping")
        return True, ""
    except Exception as e:
        return False, str(e)