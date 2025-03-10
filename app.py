from fastapi import FastAPI
import requests
import config  # Importamos las credenciales de Strava desde config.py

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "¡Hola, JogR!"}

@app.get("/strava/activities")
def get_strava_activities():
    """Obtiene las actividades del usuario autenticado en Strava"""
    headers = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    response = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)

    if response.status_code == 200:
        return response.json()
    else:
        return {
            "error": "No se pudieron obtener actividades",
            "status": response.status_code,
            "details": response.json()
        }