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

@app.post("/achievements/save")
def save_achievements(payload: dict = Body(...)):
    if db is None:
        return {"error": "Firestore no está disponible"}

    try:
        user_id = payload.get("userID")
        unlocked = payload.get("unlocked", {})  # dict con fechas ISO
        locked = payload.get("locked", [])

        if not user_id:
            return {"error": "Falta el userID"}

        # Obtener logros ya guardados para evitar duplicados
        existing_doc = db.collection("userAchievements").document(user_id).get()
        existing_unlocked = existing_doc.to_dict().get("unlocked", {}) if existing_doc.exists else {}

        # Solo añadir los nuevos que no estén ya
        for name, date in unlocked.items():
            if name not in existing_unlocked:
                existing_unlocked[name] = date

        db.collection("userAchievements").document(user_id).set({
            "unlocked": existing_unlocked,
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
