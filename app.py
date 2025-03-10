from fastapi import FastAPI
import requests
import os  # Importamos os para leer las variables de entorno

app = FastAPI()

# Leer credenciales de Strava desde las variables de entorno
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

@app.get("/")
def read_root():
    return {"message": "¡Hola, JogR!"}

@app.get("/strava/activities")
def get_strava_activities():
    """Obtiene las actividades del usuario autenticado en Strava"""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)

    if response.status_code == 200:
        return response.json()
    else:
        return {
            "error": "No se pudieron obtener actividades",
            "status": response.status_code,
            "details": response.json()
        }
}