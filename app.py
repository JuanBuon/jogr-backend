import os
import json
import time
import uuid
import logging
import requests

from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

from google.cloud import firestore
from google.oauth2 import service_account

# ——— Configuración de logging —————————————————————————
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jogr-backend")

# ——— Init FastAPI ——————————————————————————————————————
app = FastAPI()

# ——— Firestore ————————————————————————————————————————
cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not cred_json:
    raise RuntimeError("Falta GOOGLE_CREDENTIALS_JSON")

db = firestore.Client(
    credentials=service_account.Credentials.from_service_account_info(
        json.loads(cred_json)
    )
)
log.info("✅ Firestore conectado")

# ——— Strava constants ————————————————————————————————————
CLIENT_ID             = os.getenv("CLIENT_ID", "")
CLIENT_SECRET         = os.getenv("CLIENT_SECRET", "")
STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

CALLBACK_PATH  = "/auth/strava/callback"
BACKEND_ORIGIN = os.getenv("BACKEND_ORIGIN", "https://jogr-backend.onrender.com")
REDIRECT_URI   = f"{BACKEND_ORIGIN}{CALLBACK_PATH}"

# ——— Helpers Firestore ————————————————————————————————————
def oauth_doc(uid: str):
    return db.collection("users").document(uid).collection("oauth").document("strava")

def ensure_access_token(uid: str) -> str:
    doc = oauth_doc(uid).get()
    if not doc.exists:
        raise HTTPException(404, "Token Strava no encontrado")
    data = doc.to_dict()
    if time.time() > data["expires_at"] - 300:
        log.info("🔄 Refrescando token Strava para %s", uid)
        r = requests.post(STRAVA_TOKEN_URL, data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": data["refresh_token"]
        })
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
        "date":      d["date"]
    }
    if "avg_speed"        in d: out["avg_speed"]        = d["avg_speed"]
    if "summary_polyline" in d: out["summary_polyline"] = d["summary_polyline"]
    return out

# ——— Health-check —————————————————————————————————————————
@app.get("/")
def health() -> PlainTextResponse:
    return PlainTextResponse("OK", status_code=200)

# ——— Strava OAuth callback ——————————————————————————————
@app.get(CALLBACK_PATH)
def strava_callback(
    code:  str = Query(...),
    state: str = Query(None)
):
    log.info("🔑 Callback Strava recibido: code=%s state=%s", code, state)
    r = requests.post(STRAVA_TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  REDIRECT_URI
    })
    r.raise_for_status()
    tok = r.json()
    sid  = str(tok["athlete"]["id"])
    nick = tok["athlete"].get("username") or tok["athlete"].get("firstname") or "strava"
    q    = db.collection("users").where("stravaID","==",sid).get()
    uid  = q[0].id if q else str(uuid.uuid4())
    if not q:
        db.collection("users").document(uid).set({
            "userID":    uid,
            "stravaID":  sid,
            "nickname":  nick,
            "email":     "", "birthdate": "", "gender": "",
            "country":   "", "description":"", "platforms": {"strava": sid}
        })
    tok["expires_at"] = time.time() + tok["expires_in"]
    oauth_doc(uid).set(tok)
    return RedirectResponse(f"jogr://auth?userID={uid}&code={code}", status_code=302)

# ——— Strava raw activities ——————————————————————————————
@app.get("/users/{uid}/strava/activities")
def strava_activities(uid: str, per_page: int = Query(100, le=200)):
    token = ensure_access_token(uid)
    r = requests.get(STRAVA_ACTIVITIES_URL,
                     headers={"Authorization": f"Bearer {token}"},
                     params={"per_page": per_page})
    r.raise_for_status()
    arr = r.json()
    return {"activities":[
        {
            "userID":           uid,
            "id":               str(a["id"]),
            "type":             a["type"],
            "distance":         round(a["distance"]/1000,2),
            "duration":         round(a["moving_time"]/60,2),
            "elevation":        round(a["total_elevation_gain"],2),
            "avg_speed":        a.get("average_speed"),
            "summary_polyline": a["map"]["summary_polyline"],
            "date":             a["start_date"]
        }
        for a in arr if a["type"] in ("Run","Walk")
    ]}

# ——— CRUD propias ————————————————————————————————————————
@app.get("/activities/{uid}")
def activities_by_user(uid: str):
    docs = db.collection("activities").where("userID","==",uid).stream()
    return {"activities":[_fmt_act(d.to_dict()) for d in docs]}

@app.post("/activities/save")
def save_activity(p: dict = Body(...)):
    need = {"userID","id","type","distance","duration","elevation","date","includedInLeagues"}
    if not need.issubset(p):
        raise HTTPException(400,"Faltan campos")
    doc_id = f"{p['userID']}_{p['id']}"
    base   = {k:p[k] for k in ("userID","type","distance","duration","elevation","date")}
    base["activityID"]=str(p["id"])
    if "avg_speed"       in p: base["avg_speed"]=p["avg_speed"]
    if "summary_polyline"in p: base["summary_polyline"]=p["summary_polyline"]
    db.collection("activities").document(doc_id).set({**base,"includedInLeagues":p["includedInLeagues"]})
    for lg in p["includedInLeagues"]:
        db.collection("leagues").document(lg).collection("activities").document(doc_id).set(base)
    return {"success": True}

# ——— Liga: actividades con social —————————————————————————
@app.get("/league/{lid}/activities")
def league_activities(
    lid: str,
    user_id: str = Query(None, alias="userID")
):
    log.info("📥 solicitadas actividades de liga %s para user %s", lid, user_id)
    docs = db.collection("leagues").document(lid).collection("activities").stream()
    activities = []
    for d in docs:
        a = d.to_dict()
        act_id = d.id

        # likes
        likes_doc = db.collection("activities")\
                      .document(act_id)\
                      .collection("social")\
                      .document("likes").get()
        users = likes_doc.to_dict().get("users", []) if likes_doc.exists else []
        like_count = len(users)
        did_i_like = (user_id in users) if user_id else False

        # comments
        comm_docs = db.collection("activities")\
                      .document(act_id)\
                      .collection("comments").stream()
        comment_count = sum(1 for _ in comm_docs)

        entry = _fmt_act(a)
        entry.update({
            "likeCount": like_count,
            "didILike": did_i_like,
            "commentCount": comment_count
        })
        activities.append(entry)
    return {"activities": activities}

# ——— Liga: ranking ————————————————————————————————————
@app.get("/league/{lid}/ranking")
def league_ranking(
    lid: str,
    period: str = Query("general")
):
    docs = db.collection("leagues").document(lid).collection("activities").stream()
    acts = [d.to_dict() for d in docs]
    if period.lower()=="weekly":
        cutoff = datetime.utcnow()-timedelta(days=7)
        acts = [a for a in acts if datetime.fromisoformat(a["date"].replace("Z",""))>=cutoff]
    buckets=defaultdict(list)
    for a in acts: buckets[a["userID"]].append(a)

    def score(arr):
        pts=0
        dist=sum(a["distance"] for a in arr)
        pts+=min(60,int(dist))
        tm=sum(a["duration"] for a in arr)
        pace=(1/(dist/(tm/60)))*60 if tm>0 else float('inf')
        if pace<=5: pts+=60
        elif pace<7.5: pts+=round((7.5-pace)/(2.5)*60)
        elev=sum(a["elevation"] for a in arr)
        pts+=min(30,int(elev/10))
        runs=len(arr)
        pts+=min(30,runs*10)
        longest=max((a["distance"] for a in arr),default=0)
        if longest>=15: pts+=30
        elif longest>=10: pts+=20
        elif longest>=5: pts+=10
        if runs>=3: pts+=20
        return pts

    rank=[]
    for uid,arr in buckets.items():
        nick=db.collection("users").document(uid).get().to_dict().get("nickname","Usuario")
        rank.append({"userID":uid,"nickname":nick,"points":score(arr)})
    rank.sort(key=lambda x: x["points"], reverse=True)
    return {"ranking": rank}

# ——— Likes & Comments ————————————————————————————————————
@app.post("/activities/{act}/likes/{uid}")
def toggle_like(act: str, uid: str):
    ref   = db.collection("activities").document(act).collection("social").document("likes")
    doc   = ref.get()
    users = doc.to_dict().get("users", []) if doc.exists else []
    did   = uid not in users
    if did: users.append(uid)
    else:   users.remove(uid)
    ref.set({"users": users})
    return {"success": True, "didLike": did, "likeCount": len(users)}

@app.get("/activities/{act}/comments")
def get_comments(act: str):
    docs = db.collection("activities").document(act).collection("comments")\
             .order_by("date").stream()
    comments = [
        d.to_dict() | {"id": d.id, "activityID": act}
        for d in docs
    ]
    return {"comments": comments}

@app.post("/activities/{act}/comments")
def add_comment(act: str, p: dict = Body(...)):
    need = {"userID","nickname","text"}
    if not need.issubset(p):
        raise HTTPException(400,"Faltan campos")
    cid = str(uuid.uuid4())
    db.collection("activities").document(act).collection("comments").document(cid).set({
        "userID":   p["userID"],
        "nickname": p["nickname"],
        "text":     p["text"],
        "date":     datetime.utcnow().isoformat()
    })
    return {"success": True, "commentID": cid}

# ——— Achievements ——————————————————————————————————————
@app.post("/achievements/save")
def save_achievements(p: dict = Body(...)):
    uid      = p.get("userID")
    unlocked = p.get("unlocked",{})
    locked   = p.get("locked",[])
    if not uid: raise HTTPException(400,"Falta userID")
    db.collection("userAchievements").document(uid).set({
        "unlocked": unlocked, "locked": locked, "updatedAt": datetime.utcnow().isoformat()
    })
    return {"success": True}

@app.get("/achievements/{uid}")
def get_achievements(uid: str):
    d = db.collection("userAchievements").document(uid).get()
    base = {"exists": d.exists}
    return {**base, **(d.to_dict() if d.exists else {"unlocked":{}, "locked":[]})}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT","10000")), log_level="info")