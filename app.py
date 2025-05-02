import os, json, time, uuid, requests
from datetime import datetime
from collections import defaultdict

from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from google.cloud import firestore
from google.oauth2 import service_account

# ───────────────────── Init ─────────────────────
app = FastAPI()

credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not credentials_json:
    raise RuntimeError("❌ Falta GOOGLE_CREDENTIALS_JSON")

creds = service_account.Credentials.from_service_account_info(json.loads(credentials_json))
db = firestore.Client(credentials=creds)
print("✅ Firestore conectado")

CLIENT_ID  = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# ───────────── Helpers ─────────────
def oauth_doc(uid: str):
    return db.collection("users").document(uid).collection("oauth").document("strava")

def ensure_access_token(uid: str) -> str:
    doc = oauth_doc(uid).get()
    if not doc.exists:
        raise HTTPException(404, "Token de Strava no encontrado")

    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0) - 300:
        resp = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"]
        })
        resp.raise_for_status()
        fresh = resp.json()
        fresh["expires_at"] = time.time() + fresh["expires_in"]
        oauth_doc(uid).set(fresh)
        return fresh["access_token"]
    return data["access_token"]

def _fmt_activity(d: dict) -> dict:
    return {
        "userID": d.get("userID"),
        "id": str(d.get("activityID") or d.get("id")),
        "type": d.get("type"),
        "distance": d.get("distance"),
        "duration": d.get("duration"),
        "elevation": d.get("elevation"),
        "date": d.get("date")
    }

# ───────────── Strava callback ─────────────
@app.head("/")
def _head_ok():
    return PlainTextResponse("", status_code=200)

@app.get("/")
def strava_callback(code: str = Query(None)):
    if code is None:
        return PlainTextResponse("Código no proporcionado", status_code=400)

    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    })

    # ⬇️ DEBUG extra: imprime el error devuelto por Strava (400)
    if resp.status_code != 200:
        print("STRAVA ERROR:", resp.text)
        resp.raise_for_status()

    tokens = resp.json()

    strava_id = str(tokens["athlete"]["id"])
    nickname  = tokens["athlete"].get("username") or tokens["athlete"].get("firstname") or "strava_user"

    q = db.collection("users").where("stravaID", "==", strava_id).get()
    uid = q[0].id if q else str(uuid.uuid4())

    if not q:
        db.collection("users").document(uid).set({
            "userID": uid,
            "stravaID": strava_id,
            "nickname": nickname,
            "email": "", "birthdate": "", "gender": "", "country": "",
            "description": "", "platforms": {"strava": strava_id}
        })

    tokens["expires_at"] = time.time() + tokens["expires_in"]
    oauth_doc(uid).set(tokens)

    return RedirectResponse(f"jogr://auth?userID={uid}&code={code}", status_code=302)

# -------- util legacy --------
@app.get("/user/{strava_id}")
def get_user_by_strava_id(strava_id: str):
    q = db.collection("users").where("stravaID", "==", str(strava_id)).get()
    if not q:
        raise HTTPException(404, "Usuario no encontrado")
    return {"userID": q[0].id}
# -----------------------------------------------------------------

# ───────────── Actividades ─────────────
@app.get("/users/{uid}/strava/activities")
def fetch_strava_activities(uid: str, per_page: int = Query(100, le=200)):
    token = ensure_access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(STRAVA_ACTIVITIES_URL, headers=headers, params={"per_page": per_page})
    resp.raise_for_status()
    return {"activities": [
        {
            "userID": uid,
            "id": str(a["id"]),
            "type": a["type"],
            "distance": round(a["distance"]/1000, 2),
            "duration": round(a["moving_time"]/60, 2),
            "elevation": round(a["total_elevation_gain"], 2),
            "date": a["start_date"]
        }
        for a in resp.json() if a["type"] in ["Run", "Walk"]
    ]}

@app.get("/activities/{uid}")
def get_activities_by_user(uid: str):
    docs = db.collection("activities").where("userID", "==", uid).stream()
    return {"activities": [_fmt_activity(d.to_dict()) for d in docs]}

@app.post("/activities/save")
def save_activity(payload: dict = Body(...)):
    required = ["userID","id","type","distance","duration","elevation","date","includedInLeagues"]
    for f in required:
        if f not in payload:
            raise HTTPException(400, f"Falta {f}")

    doc_id = f"{payload['userID']}_{payload['id']}"
    base_doc = {
        "userID": payload["userID"],
        "activityID": str(payload["id"]),
        "type": payload["type"],
        "distance": payload["distance"],
        "duration": payload["duration"],
        "elevation": payload["elevation"],
        "date": payload["date"]
    }
    db.collection("activities").document(doc_id).set({**base_doc,
        "includedInLeagues": payload["includedInLeagues"]})

    for league in payload["includedInLeagues"]:
        db.collection("leagues").document(league).collection("activities")\
          .document(doc_id).set(base_doc)
    return {"success": True}

@app.get("/league/{league_id}/activities")
def get_league_activities(league_id: str):
    docs = db.collection("leagues").document(league_id).collection("activities").stream()
    return {"activities": [_fmt_activity(d.to_dict()) for d in docs]}

# ───────────── Ranking ─────────────
@app.get("/league/{league_id}/ranking")
def compute_league_ranking(league_id: str):
    docs = db.collection("leagues").document(league_id).collection("activities").stream()
    acts = [d.to_dict() for d in docs]

    buckets = defaultdict(list)
    for a in acts:
        buckets[a["userID"]].append(a)

    def score(arr):
        dist = sum(a["distance"] for a in arr)
        time_m = sum(a["duration"] for a in arr)
        elev = sum(a["elevation"] for a in arr)
        longest = max((a["distance"] for a in arr), default=0)
        runs = len(arr)
        spkph = (time_m > 0) and dist / (time_m / 60) or 0
        spkmpm = (spkph > 0) and (1/spkph)*60 or 0

        s = min(100, round(dist))
        s += min(50, round(max(0, (10-spkmpm)/.5)*2))
        s += min(50, runs*5)
        s += min(50, round(longest*2))
        s += min(50, round(elev/50))
        s += min(50, round(time_m/10))
        if runs >= 3: s += 20
        return s

    ranking = []
    for uid, arr in buckets.items():
        nick = db.collection("users").document(uid).get().to_dict().get("nickname","Usuario")
        ranking.append({"userID": uid, "nickname": nick, "points": score(arr)})

    ranking.sort(key=lambda x: x["points"], reverse=True)
    return {"ranking": ranking}

# ───────────── Logros ─────────────
@app.post("/achievements/save")
def save_achievements(payload: dict = Body(...)):
    uid = payload.get("userID")
    if not uid:
        raise HTTPException(400, "Falta userID")
    db.collection("userAchievements").document(uid).set({
        "unlocked": payload.get("unlocked", {}),
        "locked": payload.get("locked", []),
        "updatedAt": datetime.utcnow().isoformat()
    })
    return {"success": True}

@app.get("/achievements/{uid}")
def get_user_achievements(uid: str):
    doc = db.collection("userAchievements").document(uid).get()
    if doc.exists:
        return {"exists": True, **doc.to_dict()}
    return {"exists": False, "unlocked": {}, "locked": []}

# ───────────── Run ─────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)