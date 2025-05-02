import os
import requests
import json
import uuid
from google.cloud import firestore
from google.oauth2 import service_account
from fastapi import FastAPI, Query, Body
from fastapi.responses import RedirectResponse
from datetime import datetime, timedelta

app = FastAPI()

credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
print("📄 Credenciales recibidas (truncadas):", credentials_json[:50] if credentials_json else "Nada")

if credentials_json:
    try:
        credentials_dict = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(credentials_dict)
        db = firestore.Client(credentials=credentials)
        print("✅ Conexión a Firestore exitosa.")
    except Exception as e:
        print(f"❌ Error al conectar con Firestore: {e}")
        db = None
else:
    print("❌ No se encontraron credenciales de Google Cloud en las variables de entorno.")
    db = None

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")

@app.get("/")
def strava_callback(code: str = Query(None)):
    if code is None:
        return {"error": "Código de autorización no proporcionado"}

    print("🔁 Código recibido:", code)

    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }
    )

    if response.status_code != 200:
        print(f"❌ Error autenticando con Strava: {response.json()}")
        return {"error": "No se pudo autenticar con Strava", "details": response.json()}

    tokens = response.json()
    if "access_token" not in tokens or "refresh_token" not in tokens or "athlete" not in tokens:
        return {"error": "Respuesta de Strava incompleta", "details": tokens}

    strava_id = str(tokens["athlete"]["id"])
    nickname = tokens["athlete"].get("username") or tokens["athlete"].get("firstname") or "strava_user"

    if db:
        user_query = db.collection("users").where("stravaID", "==", strava_id).get()
        if not user_query:
            userID = str(uuid.uuid4())
            db.collection("users").document(userID).set({
                "userID": userID,
                "stravaID": strava_id,
                "nickname": nickname,
                "email": "",
                "birthdate": "",
                "gender": "",
                "country": "",
                "description": "",
                "platforms": {
                    "strava": strava_id
                }
            })
            print(f"✅ Usuario nuevo creado con userID interno: {userID}")
        else:
            print(f"👤 Usuario ya registrado con Strava ID: {strava_id}")

        db.collection("config").document("strava").set({
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "athlete": tokens["athlete"]
        }, merge=True)
        print("🔐 Tokens guardados correctamente.")

    return RedirectResponse(url=f"jogr://auth?code={code}")

@app.get("/user")
def get_user_id():
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        doc = db.collection("config").document("strava").get()
        if not doc.exists:
            return {"error": "Documento 'strava' no encontrado"}

        strava_id = str(doc.to_dict()["athlete"]["id"])
        users = db.collection("users").where("stravaID", "==", strava_id).get()

        for user in users:
            user_data = user.to_dict()
            return {"userID": user_data.get("userID")}

        return {"error": "No se encontró un usuario con ese Strava ID"}

    except Exception as e:
        print(f"❌ Error leyendo el documento de usuario: {e}")
        return {"error": "Error accediendo a Firestore"}
@app.get("/strava/activities")
def fetch_activities():
    if db is None:
        return {"error": "Firestore no está disponible"}

    config_doc = db.collection("config").document("strava").get()
    if not config_doc.exists:
        return {"error": "No hay token de acceso guardado"}

    saved = config_doc.to_dict()
    access_token = saved.get("access_token")

    if not access_token:
        return {"error": "Token de acceso no encontrado"}

    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)

    if response.status_code != 200:
        return {"error": "Error al obtener actividades", "status": response.status_code}

    activities = response.json()
    filtered = [
        {
            "userID": saved["athlete"]["id"],
            "id": a["id"],
            "type": a["type"],
            "distance": round(a["distance"] / 1000, 2),
            "duration": round(a["moving_time"] / 60, 2),
            "elevation": round(a["total_elevation_gain"], 2),
            "date": a["start_date"]
        }
        for a in activities if a["type"] in ["Run", "Walk"]
    ]
    return {"activities": filtered}

@app.get("/activities/{user_id}")
def get_activities_by_user(user_id: str):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        docs = db.collection("activities").where("userID", "==", user_id).stream()
        activities = []
        for doc in docs:
            data = doc.to_dict()
            activities.append({
                "userID": data.get("userID"),
                "id": data.get("activityID"),
                "type": data.get("type"),
                "distance": data.get("distance"),
                "duration": data.get("duration"),
                "elevation": data.get("elevation"),
                "date": data.get("date")
            })

        print(f"📦 Actividades devueltas para el usuario {user_id}: {len(activities)}")
        return {"activities": activities}

    except Exception as e:
        print(f"❌ Error al obtener actividades del usuario: {e}")
        return {"error": "Error accediendo a Firestore", "details": str(e)}

@app.get("/league/{league_id}/ranking")
def compute_league_ranking(league_id: str):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        docs = db.collection("leagues").document(league_id).collection("activities").stream()
        league_activities = [doc.to_dict() for doc in docs]

        from collections import defaultdict
        user_data = defaultdict(list)
        for a in league_activities:
            user_data[a["userID"]].append(a)

        def calculate_points(user_activities):
            total_distance = sum(a["distance"] for a in user_activities)
            total_duration = sum(a["duration"] for a in user_activities)
            total_elevation = sum(a["elevation"] for a in user_activities)
            num_runs = len(user_activities)
            longest_run = max((a["distance"] for a in user_activities), default=0)
            avg_speed_kph = (total_duration > 0) and (total_distance / (total_duration / 60)) or 0
            avg_speed_kmpm = (avg_speed_kph > 0) and ((1 / avg_speed_kph) * 60) or 0

            score = 0
            score += min(100, round(total_distance * 1))
            score += min(50, round(max(0, (10 - avg_speed_kmpm) / 0.5) * 2))
            score += min(50, num_runs * 5)
            score += min(50, round(longest_run * 2))
            score += min(50, round(total_elevation / 50))
            score += min(50, round(total_duration / 10))
            if num_runs >= 3:
                score += 20
            return score

        ranking = []
        for user_id, acts in user_data.items():
            points = calculate_points(acts)
            nickname = "Usuario"
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_info = user_doc.to_dict()
                nickname = user_info.get("nickname", "Usuario")

            ranking.append({
                "userID": user_id,
                "points": points,
                "nickname": nickname
            })

        ranking.sort(key=lambda x: x["points"], reverse=True)
        return {"ranking": ranking}

    except Exception as e:
        print(f"❌ Error calculando ranking de liga: {e}")
        return {"error": "Error accediendo a Firestore", "details": str(e)}

@app.post("/activities/save")
def save_activity(payload: dict = Body(...)):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        required_fields = ["userID", "id", "type", "distance", "duration", "elevation", "date", "includedInLeagues"]
        for field in required_fields:
            if field not in payload:
                return {"error": f"Falta el campo obligatorio: {field}"}

        activity_id = str(payload["id"])
        document_id = f"{payload['userID']}_{activity_id}"

        db.collection("activities").document(document_id).set({
            "userID": payload["userID"],
            "activityID": activity_id,
            "type": payload["type"],
            "distance": payload["distance"],
            "duration": payload["duration"],
            "elevation": payload["elevation"],
            "date": payload["date"],
            "includedInLeagues": payload["includedInLeagues"]
        })

        for league_id in payload["includedInLeagues"]:
            db.collection("leagues").document(league_id).collection("activities").document(document_id).set({
                "userID": payload["userID"],
                "activityID": activity_id,
                "type": payload["type"],
                "distance": payload["distance"],
                "duration": payload["duration"],
                "elevation": payload["elevation"],
                "date": payload["date"]
            })

        print(f"✅ Actividad guardada: {document_id}")
        return {"success": True, "message": "Actividad guardada correctamente"}

    except Exception as e:
        print(f"❌ Error guardando actividad: {e}")
        return {"error": "Error al guardar la actividad", "details": str(e)}
@app.get("/league/{league_id}/activities")
def get_league_activities(league_id: str):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        docs = db.collection("leagues").document(league_id).collection("activities").stream()
        activities = []
        for doc in docs:
            data = doc.to_dict()
            activities.append({
                "userID": data.get("userID"),
                "id": data.get("activityID"),
                "type": data.get("type"),
                "distance": data.get("distance"),
                "duration": data.get("duration"),
                "elevation": data.get("elevation"),
                "date": data.get("date")
            })

        print(f"📥 Actividades devueltas para liga {league_id}: {len(activities)}")
        return {"activities": activities}

    except Exception as e:
        print(f"❌ Error al leer actividades de liga: {e}")
        return {"error": "Error accediendo a Firestore", "details": str(e)}

@app.post("/achievements/save")
def save_achievements(payload: dict = Body(...)):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        user_id = payload.get("userID")
        unlocked = payload.get("unlocked", {})  # dict con fechas
        locked = payload.get("locked", [])

        if not user_id:
            return {"error": "Falta el userID"}

        db.collection("userAchievements").document(user_id).set({
            "unlocked": unlocked,
            "locked": locked,
            "updatedAt": datetime.utcnow().isoformat()
        })

        print(f"✅ Logros guardados para el usuario {user_id}")
        return {"success": True, "message": "Logros guardados correctamente"}

    except Exception as e:
        print(f"❌ Error guardando logros: {e}")
        return {"error": "Error al guardar los logros", "details": str(e)}

@app.get("/achievements/{user_id}")
def get_user_achievements(user_id: str):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        doc = db.collection("userAchievements").document(user_id).get()
        if doc.exists:
            return {"exists": True, **doc.to_dict()}
        else:
            return {"exists": False, "unlocked": {}, "locked": []}
    except Exception as e:
        print(f"❌ Error al obtener logros: {e}")
        return {"error": "Error accediendo a Firestore", "details": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)