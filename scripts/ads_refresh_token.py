"""
Genera el REFRESH TOKEN de Google Ads (se ejecuta UNA vez, en tu ordenador).

1) pip install google-auth-oauthlib python-dotenv
2) CLIENT_ID y CLIENT_SECRET (cliente OAuth "App de escritorio" de Google Cloud
   Console → APIs y servicios → Credenciales). Puedes exportarlos como variables de
   entorno GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET (o ponerlos en .env), o
   rellenarlos abajo a mano.
3) python scripts/ads_refresh_token.py
4) Se abre el navegador: entra con la cuenta de Google que tiene acceso a la MCC
   y acepta. Al final imprime el REFRESH TOKEN → pásamelo (o guárdalo como secreto).

⚠️ CAUSA DURABLE del "invalid_grant: Token expired or revoked": si la pantalla de
   consentimiento OAuth está en modo "Testing", Google CADUCA el refresh token cada
   7 días. Arréglalo de raíz: Cloud Console → Pantalla de consentimiento OAuth →
   PUBLICAR APP (estado "En producción"). Luego regenera el token UNA vez con esto.
"""
import os
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID") or "PON_AQUI_TU_CLIENT_ID.apps.googleusercontent.com"
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET") or "PON_AQUI_TU_CLIENT_SECRET"

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
