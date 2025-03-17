import os
import requests
import json
from google.cloud import firestore
from google.oauth2 import service_account
from fastapi import FastAPI, Query

app = FastAPI()

# Cargar credenciales de Firebase desde la variable de entorno
credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

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

# Cargar credenciales de Strava desde las variables de entorno
CLIENT_ID = os.getenv("CLIENT_ID", "151673")  # ⚠️ Cambia si es otro ID
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "4f3e5a80e4810ad27b161b63730590c9a0d30051")

def get_saved_tokens():
    """Obtiene los tokens de Strava guardados en Firestore."""
    if db is None:
        print("⚠️ Firestore no está disponible.")
        return None

    try:
        doc = db.collection("config").document("strava").get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"❌ Error obteniendo los tokens: {e}")
        return None

def refresh_access_token():
    """Refresca el Access Token y lo guarda en Firestore."""
    saved_tokens = get_saved_tokens()
    if not saved_tokens or "refresh_token" not in saved_tokens:
        print("⚠️ No hay refresh_token guardado en Firestore.")
        return None

    refresh_token = saved_tokens["refresh_token"]
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
    )

    if response.status_code == 200:
        new_tokens = response.json()
        if "access_token" in new_tokens and "refresh_token" in new_tokens:
            if db:
                db.collection("config").document("strava").set({
                    "access_token": new_tokens["access_token"],
                    "refresh_token": new_tokens["refresh_token"]
                }, merge=True)
                print("🔄 Access Token refrescado y guardado en Firestore.")
            return new_tokens["access_token"]
        else:
            print("❌ Error: Strava no devolvió tokens válidos.")
            return None
    else:
        print(f"❌ Error refrescando token: {response.json()}")
        return None

def get_strava_activities():
    """Obtiene actividades recientes de Strava."""
    if db is None:
        return {"error": "Firestore no está disponible"}

    saved_tokens = get_saved_tokens()
    if not saved_tokens or "access_token" not in saved_tokens:
        print("⚠️ No hay access_token guardado. Intentando refrescar...")
        access_token = refresh_access_token()
        if not access_token:
            return {"error": "No se pudo refrescar el token de acceso"}
    else:
        access_token = saved_tokens["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)

    if response.status_code == 401:
        print("⚠️ Access Token inválido. Intentando refrescar...")
        access_token = refresh_access_token()
        if not access_token:
            return {"error": "No se pudo refrescar el token de acceso"}
        
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)

    if response.status_code != 200:
        return {"error": "No se pudieron obtener actividades", "status": response.status_code, "details": response.json()}

    activities = response.json()
    filtered_activities = [
        {
            "id": activity["id"],
            "type": activity["type"],
            "distance": round(activity["distance"] / 1000, 2),  # Metros a km
            "duration": round(activity["moving_time"] / 60, 2),  # Segundos a minutos
            "elevation": round(activity["total_elevation_gain"], 2),
            "date": activity["start_date"]
        }
        for activity in activities if activity["type"] in ["Run", "Walk"]
    ]
    return {"activities": filtered_activities}

@app.get("/strava/activities")
def fetch_activities():
    return get_strava_activities()

@app.get("/callback")
def strava_callback(code: str = Query(...)):
    """Recibe el código de autorización de Strava y obtiene los tokens."""
    if db is None:
        return {"error": "Firestore no está disponible"}

    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }
    )

    if response.status_code == 200:
        tokens = response.json()
        
        if "access_token" in tokens and "refresh_token" in tokens:
            db.collection("config").document("strava").set({
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"]
            }, merge=True)
            print("✅ Tokens de Strava guardados en Firestore.")
            return {"message": "Autenticación con Strava exitosa"}
        else:
            return {"error": "Respuesta de Strava incompleta", "details": tokens}
    else:
        return {"error": "No se pudo autenticar con Strava", "details": response.json()}

# Iniciar el servidor correctamente
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)