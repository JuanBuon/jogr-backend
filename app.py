import os
import requests
import json
import uuid
from google.cloud import firestore
from google.oauth2 import service_account
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse

app = FastAPI()

# Cargar credenciales de Firebase desde la variable de entorno
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
    nickname = tokens["athlete"].get("username", "strava_user")

    if db:
        user_ref = db.collection("users").document(strava_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            userID = str(uuid.uuid4())  # ID único interno de JogR
            user_ref.set({
                "userID": userID,
                "stravaID": strava_id,
                "nickname": nickname,
                "platforms": {
                    "strava": strava_id
                }
            })
            print(f"✅ Usuario nuevo creado con userID interno: {userID}")
        else:
            print(f"👤 Usuario ya registrado con Strava ID: {strava_id}")

        # Guardar tokens en config/strava
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)