import os, json, time, uuid, requests
from datetime import datetime
from collections import defaultdict

from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

from google.cloud import firestore
from google.oauth2 import service_account

# ───────────────────────── Init ─────────────────────────
app = FastAPI()

# Credenciales de Firestore
cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not cred_json:
    raise RuntimeError("Falta GOOGLE_CREDENTIALS_JSON")

db = firestore.Client(
    credentials=service_account.Credentials.from_service_account_info(
        json.loads(cred_json)
    )
)
print("✅ Firestore conectado")

# Cliente OAuth Strava
CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("REDIRECT_URI", "")

if REDIRECT_URI == "":
    raise RuntimeError("Falta REDIRECT_URI (ej: https://jogr-backend.onrender.com/)")

STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# ───────────────────────── Helpers ──────────────────────
def oauth_doc(uid: str):
    return db.collection("users").document(uid).collection("oauth").document("strava")

def ensure_access_token(uid: str) -> str:
    doc = oauth_doc(uid).get()
    if not doc.exists:
        raise HTTPException(404, "Token Strava no encontrado")

    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0) - 300:
        r = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "refresh_token",
                "refresh_token": data["refresh_token"],
                "redirect_uri":  REDIRECT_URI
            },
        )
        r.raise_for_status()
        fresh = r.json()
        fresh["expires_at"] = time.time() + fresh["expires_in"]
        oauth_doc(uid).set(fresh)
        return fresh["access_token"]

    return data["access_token"]

def _fmt_act(d: dict) -> dict:
    out = {
        "userID":    d["userID"],
        "id":        str(d.get("activityID") or d.get("id")),
        "type":      d["type"],
        "distance":  d["distance"],
        "duration":  d["duration"],
        "elevation": d["elevation"],
        "date":      d["date"],
    }
    if "avg_speed" in d:
        out["avg_speed"] = d["avg_speed"]
    if "summary_polyline" in d:
        out["summary_polyline"] = d["summary_polyline"]
    return out

# ───────────────────── Strava OAuth callback ─────────────
@app.head("/")  # health-check
def _head():
    return PlainTextResponse("", 200)

@app.get("/")
def strava_callback(code: str = Query(None)):
    if not code:
        return PlainTextResponse("Código no proporcionado", 400)

    r = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  REDIRECT_URI
        },
    )
    r.raise_for_status()
    tok  = r.json()

    sid  = str(tok["athlete"]["id"])
    nick = tok["athlete"].get("username") or tok["athlete"].get("firstname") or "strava_user"

    q = db.collection("users").where("stravaID", "==", sid).get()
    if q:
        uid = q[0].id
    else:
        uid = str(uuid.uuid4())
        db.collection("users").document(uid).set({
            "userID":      uid,
            "stravaID":    sid,
            "nickname":    nick,
            "email":       "",
            "birthdate":   "",
            "gender":      "",
            "country":     "",
            "description": "",
            "platforms":   {"strava": sid}
        })

    tok["expires_at"] = time.time() + tok["expires_in"]
    oauth_doc(uid).set(tok)

    return RedirectResponse(f"jogr://auth?userID={uid}&code={code}", status_code=302)

# ───────────────────── Strava activities raw ─────────────
@app.get("/users/{uid}/strava/activities")
def strava_activities(uid: str, per_page: int = Query(100, le=200)):
    token = ensure_access_token(uid)
    r = requests.get(
        STRAVA_ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": per_page},
    )
    r.raise_for_status()
    return {
        "activities": [
            {
                "userID":           uid,
                "id":               str(a["id"]),
                "type":             a["type"],
                "distance":         round(a["distance"] / 1000, 2),
                "duration":         round(a["moving_time"] / 60, 2),
                "elevation":        round(a["total_elevation_gain"], 2),
                "avg_speed":        a.get("average_speed"),
                "summary_polyline": a["map"]["summary_polyline"],
                "date":             a["start_date"],
            }
            for a in r.json() if a["type"] in ("Run", "Walk")
        ]
    }

# ───────────────────── CRUD actividades guardadas ────────
@app.get("/activities/{uid}")
def activities_by_user(uid: str):
    docs = db.collection("activities").where("userID", "==", uid).stream()
    return {"activities": [_fmt_act(d.to_dict()) for d in docs]}

@app.post("/activities/save")
def save_activity(p: dict = Body(...)):
    need = {"userID","id","type","distance","duration","elevation","date","includedInLeagues"}
    if not need.issubset(p):
        raise HTTPException(400, "Faltan campos requeridos")

    doc_id = f"{p['userID']}_{p['id']}"
    base   = {k: p[k] for k in ("userID","type","distance","duration","elevation","date")}
    base["activityID"] = str(p["id"])
    if "avg_speed" in p:
        base["avg_speed"] = p["avg_speed"]
    if "summary_polyline" in p:
        base["summary_polyline"] = p["summary_polyline"]

    db.collection("activities").document(doc_id).set({
        **base,
        "includedInLeagues": p["includedInLeagues"]
    })

    for lg in p["includedInLeagues"]:
        db.collection("leagues").document(lg)\
          .collection("activities").document(doc_id).set(base)

    return {"success": True}

# ───────────────────── Liga: actividades & ranking ───────
@app.get("/league/{lid}/activities")
def league_activities(lid: str):
    docs = db.collection("leagues").document(lid).collection("activities").stream()
    return {"activities": [_fmt_act(d.to_dict()) for d in docs]}

@app.get("/league/{lid}/ranking")
def league_ranking(lid: str):
    docs = db.collection("leagues").document(lid).collection("activities").stream()
    acts = [d.to_dict() for d in docs]
    buckets = defaultdict(list)
    for a in acts:
        buckets[a["userID"]].append(a)

    def score(arr):
        dist = sum(x["distance"] for x in arr)
        time_m = sum(x["duration"] for x in arr)
        elev = sum(x["elevation"] for x in arr)
        runs = len(arr)
        longest = max((x["distance"] for x in arr), default=0)
        spkph = dist / (time_m/60) if time_m>0 else 0
        spkmpm = (1/spkph)*60 if spkph>0 else 0
        s  = min(100, round(dist))
        s += min(50, round(max(0,(10-spkmpm)/0.5)*2))
        s += min(50, runs*5)
        s += min(50, round(longest*2))
        s += min(50, round(elev/50))
        s += min(50, round(time_m/10))
        if runs>=3: s += 20
        return s

    ranking = []
    for uid, arr in buckets.items():
        nick = db.collection("users").document(uid).get().to_dict().get("nickname","Usuario")
        ranking.append({"userID":uid,"nickname":nick,"points":score(arr)})
    ranking.sort(key=lambda x: x["points"], reverse=True)
    return {"ranking": ranking}

# ───────────────────── Likes & Comments ──────────────────
@app.post("/activities/{act}/likes/{uid}")
def toggle_like(act: str, uid: str):
    ref = db.collection("activities").document(act).collection("social").document("likes")
    doc = ref.get()
    likes = doc.to_dict().get("users", []) if doc.exists else []
    if uid in likes:
        likes.remove(uid); did = False
    else:
        likes.append(uid); did = True
    ref.set({"users": likes})
    return {"success": True, "didLike": did, "likeCount": len(likes)}

@app.get("/activities/{act}/comments")
def get_comments(act: str):
    docs = db.collection("activities").document(act).collection("comments").order_by("date").stream()
    return {"comments": [d.to_dict() | {"id": d.id} for d in docs]}

@app.post("/activities/{act}/comments")
def add_comment(act: str, p: dict = Body(...)):
    need = {"userID","nickname","text"}
    if not need.issubset(p):
        raise HTTPException(400,"Faltan campos")
    cid = str(uuid.uuid4())
    db.collection("activities").document(act).collection("comments").document(cid).set({
        "userID": p["userID"],
        "nickname": p["nickname"],
        "text": p["text"],
        "date": datetime.utcnow().isoformat()
    })
    return {"success": True, "commentID": cid}

# ───────────────────── Achievements ──────────────────────
@app.post("/achievements/save")
def save_achievements(p: dict = Body(...)):
    uid      = p.get("userID")
    unlocked = p.get("unlocked", {})
    locked   = p.get("locked", [])
    if not uid:
        raise HTTPException(400,"Falta userID")
    db.collection("userAchievements").document(uid).set({
        "unlocked": unlocked,
        "locked": locked,
        "updatedAt": datetime.utcnow().isoformat()
    })
    return {"success": True}

@app.get("/achievements/{uid}")
def get_achievements(uid: str):
    d = db.collection("userAchievements").document(uid).get()
    return {"exists": d.exists, **(d.to_dict() if d.exists else {"unlocked": {}, "locked": []})}

# ───────────────────────── Run ────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)