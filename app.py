import os
import json
import time
import uuid
from datetime import datetime
from collections import defaultdict

import requests
from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from google.cloud import firestore
from google.oauth2 import service_account

# ───────────────────────── Init ──────────────────────────
app = FastAPI()

credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not credentials_json:
    raise RuntimeError("❌ Falta la variable GOOGLE_CREDENTIALS_JSON")

credentials_dict = json.loads(credentials_json)
credentials = service_account.Credentials.from_service_account_info(credentials_dict)
db = firestore.Client(credentials=credentials)
print("✅ Firestore conectado")

CLIENT_ID       = os.getenv("CLIENT_ID", "")
CLIENT_SECRET   = os.getenv("CLIENT_SECRET", "")
STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# ───────────────────── Helpers ───────────────────────────
def oauth_doc(user_id: str):
    return (
        db.collection("users")
          .document(user_id)
          .collection("oauth")
          .document("strava")
    )

def ensure_access_token(user_id: str) -> str:
    """Devuelve access_token válido, refrescándolo si caducó."""
    doc = oauth_doc(user_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Token de Strava no encontrado")

    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0) - 300:
        print(f"🔄 Refrescando token para {user_id}")
        resp = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"]
        })
        resp.raise_for_status()
        fresh = resp.json()
        fresh["expires_at"] = time.time() + fresh["expires_in"]
        oauth_doc(user_id).set(fresh)
        return fresh["access_token"]

    return data["access_token"]

# ───────────────────── Root callback ─────────────────────
@app.head("/")
def strava_head():
    """Safari/Chrome envían HEAD antes del GET: damos 200 para evitar 405."""
    return PlainTextResponse("", status_code=200)

@app.get("/")
def strava_callback(code: str = Query(None)):
    if code is None:
        return PlainTextResponse("Código de autorización no proporcionado", status_code=400)

    # 1. Intercambiar código por tokens
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    })
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())

    tokens = resp.json()
    strava_id = str(tokens["athlete"]["id"])
    nickname  = tokens["athlete"].get("username") or tokens["athlete"].get("firstname") or "strava_user"

    # 2. Crear / obtener usuario interno
    q = db.collection("users").where("stravaID", "==", strava_id).get()
    if q:
        user_id = q[0].id
    else:
        user_id = str(uuid.uuid4())
        db.collection("users").document(user_id).set({
            "userID": user_id,
            "stravaID": strava_id,
            "nickname": nickname,
            "email": "",
            "birthdate": "",
            "gender": "",
            "country": "",
            "description": "",
            "platforms": {"strava": strava_id}
        })

    # 3. Guardar tokens por usuario
    tokens["expires_at"] = time.time() + tokens["expires_in"]
    oauth_doc(user_id).set(tokens)

    # 4. Redirigir a JogR con **userID y code** (302)
    return RedirectResponse(
        url=f"jogr://auth?userID={user_id}&code={code}",
        status_code=302
    )

# ---------- util legacy -------------
@app.get("/user/{strava_id}")
def get_user_by_strava_id(strava_id: str):
    q = db.collection("users").where("stravaID", "==", str(strava_id)).get()
    if not q:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {"userID": q[0].id}
# ------------------------------------

# ─────────── Resto de endpoints (sin cambios funcionales) ──────────
@app.get("/users/{user_id}/strava/activities")
def fetch_strava_activities(user_id: str, per_page: int = Query(100, le=200)):
    access = ensure_access_token(user_id)
    headers = {"Authorization": f"Bearer {access}"}
    resp = requests.get(STRAVA_ACTIVITIES_URL, headers=headers, params={"per_page": per_page})
    resp.raise_for_status()
    return {
        "activities": [
            {
                "userID": user_id,
                "id": a["id"],
                "type": a["type"],
                "distance": round(a["distance"] / 1000, 2),
                "duration": round(a["moving_time"] / 60, 2),
                "elevation": round(a["total_elevation_gain"], 2),
                "date": a["start_date"]
            }
            for a in resp.json() if a["type"] in ["Run", "Walk"]
        ]
    }

@app.get("/activities/{user_id}")
def get_activities_by_user(user_id: str):
    docs = db.collection("activities").where("userID", "==", user_id).stream()
    return {"activities": [doc.to_dict() for doc in docs]}

@app.post("/activities/save")
def save_activity(payload: dict = Body(...)):
    required = ["userID","id","type","distance","duration","elevation","date","includedInLeagues"]
    for f in required:
        if f not in payload:
            raise HTTPException(status_code=400, detail=f"Falta {f}")

    doc_id = f"{payload['userID']}_{payload['id']}"
    db.collection("activities").document(doc_id).set({
        "userID": payload["userID"],
        "activityID": str(payload["id"]),
        "type": payload["type"],
        "distance": payload["distance"],
        "duration": payload["duration"],
        "elevation": payload["elevation"],
        "date": payload["date"],
        "includedInLeagues": payload["includedInLeagues"]
    })

    for league_id in payload["includedInLeagues"]:
        db.collection("leagues").document(league_id) \
          .collection("activities").document(doc_id).set({
              "userID": payload["userID"],
              "activityID": str(payload["id"]),
              "type": payload["type"],
              "distance": payload["distance"],
              "duration": payload["duration"],
              "elevation": payload["elevation"],
              "date": payload["date"]
          })
    return {"success": True}

@app.get("/league/{league_id}/activities")
def get_league_activities(league_id: str):
    docs = db.collection("leagues").document(league_id).collection("activities").stream()
    return {"activities": [doc.to_dict() for doc in docs]}

@app.get("/league/{league_id}/ranking")
def compute_league_ranking(league_id: str):
    docs = db.collection("leagues").document(league_id).collection("activities").stream()
    acts = [d.to_dict() for d in docs]

    user_data = defaultdict(list)
    for a in acts:
        user_data[a["userID"]].append(a)

    def score(acts):
        d = sum(a["distance"] for a in acts)
        t = sum(a["duration"] for a in acts)
        e = sum(a["elevation"] for a in acts)
        n = len(acts)
        longest = max((a["distance"] for a in acts), default=0)
        spkph = (t > 0) and (d / (t / 60)) or 0
        spkmpm = (spkph > 0) and ((1/spkph)*60) or 0
        s  = min(100, round(d))
        s += min(50, round(max(0, (10 - spkmpm) / .5) * 2))
        s += min(50, n * 5)
        s += min(50, round(longest * 2))
        s += min(50, round(e / 50))
        s += min(50, round(t / 10))
        if n >= 3: s += 20
        return s

    ranking = []
    for uid, acts in user_data.items():
        nickname = db.collection("users").document(uid).get().to_dict().get("nickname", "Usuario")
        ranking.append({"userID": uid, "nickname": nickname, "points": score(acts)})

    ranking.sort(key=lambda x: x["points"], reverse=True)
    return {"ranking": ranking}

@app.post("/achievements/save")
def save_achievements(payload: dict = Body(...)):
    uid = payload.get("userID")
    if not uid:
        raise HTTPException(status_code=400, detail="Falta userID")
    db.collection("userAchievements").document(uid).set({
        "unlocked": payload.get("unlocked", {}),
        "locked": payload.get("locked", []),
        "updatedAt": datetime.utcnow().isoformat()
    })
    return {"success": True}

@app.get("/achievements/{user_id}")
def get_user_achievements(user_id: str):
    doc = db.collection("userAchievements").document(user_id).get()
    if doc.exists:
        return {"exists": True, **doc.to_dict()}
    return {"exists": False, "unlocked": {}, "locked": []}

# ──────────────────────── Run ────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)