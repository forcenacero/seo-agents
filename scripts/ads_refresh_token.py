"""
Genera el REFRESH TOKEN de Google Ads (se ejecuta UNA vez, en tu ordenador).

1) pip install google-auth-oauthlib
2) Rellena CLIENT_ID y CLIENT_SECRET (los del cliente OAuth "App de escritorio"
   que creas en Google Cloud Console → APIs y servicios → Credenciales).
3) python scripts/ads_refresh_token.py
4) Se abre el navegador: entra con la cuenta de Google que tiene acceso a la MCC
   y acepta. Al final imprime el REFRESH TOKEN → pásamelo (o guárdalo como secreto).
"""
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = "PON_AQUI_TU_CLIENT_ID.apps.googleusercontent.com"
CLIENT_SECRET = "PON_AQUI_TU_CLIENT_SECRET"

flow = InstalledAppFlow.from_client_config(
    {"installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }},
    scopes=["https://www.googleapis.com/auth/adwords"],
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\n================ COPIA ESTO ================")
print("REFRESH TOKEN:", creds.refresh_token)
print("============================================")
