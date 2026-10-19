"""
One image, three microservices. SERVICE=user | txn | auth selects the routes.

Every MongoDB call goes through run(), which:
  * holds a lock so only ONE Mongo operation is in flight per pod (clean labels),
  * records wall-clock ns timestamps immediately before/after the call,
  * prints a ground-truth JSON line to stdout (collected later with kubectl logs).

Because the capture runs on the MongoDB pod's interface, every packet belonging to
an operation is guaranteed to fall inside [t_start, t_end]: the request leaves the
client after t_start and the response reaches the client before t_end.
"""
import json
import os
import random
import socket
import threading
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

SERVICE = os.environ.get("SERVICE", "user")
MONGO_URI = os.environ.get(
    "MONGO_URI", "mongodb://mongo.fingerprint.svc.cluster.local:27017/?tls=true"
)
MONGO_CA = os.environ.get("MONGO_CA", "/etc/mongo-ca/ca.pem")
POD_IP = os.environ.get("POD_IP") or socket.gethostbyname(socket.gethostname())

client = MongoClient(
    MONGO_URI,
    tlsCAFile=MONGO_CA,
    maxPoolSize=1,            # a single app connection -> single TCP flow per pod
    minPoolSize=1,
    heartbeatFrequencyMS=60000,  # fewer monitoring packets in the capture
    serverSelectionTimeoutMS=30000,
)
db = client["shop"]
lock = threading.Lock()
app = FastAPI(title=f"{SERVICE}-api")


def run(op: str, fn):
    with lock:
        t0 = time.time_ns()
        result = fn()
        t1 = time.time_ns()
    print(
        json.dumps(
            {"gt": 1, "service": SERVICE, "op": op, "pod_ip": POD_IP,
             "t_start": t0, "t_end": t1}
        ),
        flush=True,
    )
    return result


@app.on_event("startup")
def warm_up():
    # Open the pooled TLS connection now so no handshake lands inside a labelled window.
    for _ in range(30):
        try:
            client.admin.command("ping")
            return
        except Exception:
            time.sleep(2)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": SERVICE}


# --------------------------------------------------------------------------- user
if SERVICE == "user":

    class UserIn(BaseModel):
        name: str
        email: str
        bio: str = ""

    class UserPatch(BaseModel):
        bio: str

    @app.post("/users")
    def create_user(u: UserIn):
        doc = {"_id": uuid.uuid4().hex, **u.model_dump(), "created": time.time()}
        run("insert", lambda: db.users.insert_one(doc))
        return {"id": doc["_id"]}

    @app.get("/users/{uid}")
    def get_user(uid: str):
        doc = run("find_one", lambda: db.users.find_one({"_id": uid}))
        if not doc:
            raise HTTPException(404)
        return doc

    @app.put("/users/{uid}")
    def update_user(uid: str, p: UserPatch):
        r = run("update", lambda: db.users.update_one(
            {"_id": uid}, {"$set": {"bio": p.bio, "updated": time.time()}}))
        return {"matched": r.matched_count}

    @app.delete("/users/{uid}")
    def delete_user(uid: str):
        r = run("delete", lambda: db.users.delete_one({"_id": uid}))
        return {"deleted": r.deleted_count}


# ---------------------------------------------------------------------------- txn
if SERVICE == "txn":

    class TxnIn(BaseModel):
        user_id: str
        amount: float
        merchant: str
        note: str = ""

    @app.post("/transactions")
    def create_txn(t: TxnIn):
        doc = {"_id": uuid.uuid4().hex, **t.model_dump(), "ts": time.time()}
        run("insert", lambda: db.transactions.insert_one(doc))
        return {"id": doc["_id"]}

    @app.get("/transactions")
    def list_txns(user_id: str, limit: int = 20):
        docs = run("find_many", lambda: list(
            db.transactions.find({"user_id": user_id}).limit(limit)))
        return {"count": len(docs), "items": docs}

    @app.get("/transactions/summary/{user_id}")
    def summary(user_id: str):
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": "$merchant", "total": {"$sum": "$amount"},
                        "n": {"$sum": 1}}},
            {"$sort": {"total": -1}},
        ]
        rows = run("aggregate", lambda: list(db.transactions.aggregate(pipeline)))
        return {"groups": rows}


# --------------------------------------------------------------------------- auth
if SERVICE == "auth":

    class SessionIn(BaseModel):
        user_id: str
        user_agent: Optional[str] = None

    @app.post("/sessions")
    def create_session(s: SessionIn):
        token = uuid.uuid4().hex + uuid.uuid4().hex[: random.randint(0, 32)]
        doc = {"_id": token, **s.model_dump(), "expires": time.time() + 3600}
        run("insert", lambda: db.sessions.insert_one(doc))
        return {"token": token}

    @app.get("/sessions/{token}")
    def get_session(token: str):
        doc = run("find_one", lambda: db.sessions.find_one({"_id": token}))
        if not doc:
            raise HTTPException(404)
        return doc

    @app.put("/sessions/{token}")
    def refresh_session(token: str):
        r = run("update", lambda: db.sessions.update_one(
            {"_id": token}, {"$set": {"expires": time.time() + 3600}}))
        return {"matched": r.matched_count}

    @app.delete("/sessions/{token}")
    def delete_session(token: str):
        r = run("delete", lambda: db.sessions.delete_one({"_id": token}))
        return {"deleted": r.deleted_count}
