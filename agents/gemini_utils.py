"""
Utilidades compartidas para llamar a Gemini de forma robusta.

El free-tier de Gemini limita a 5 peticiones/min por modelo. Sin reintentos,
un único 429 (ResourceExhausted) mata el agente. Este helper reintenta con
backoff, respetando el retry_delay que devuelve la propia API.
"""
import os
import re
import time

try:
    from google.api_core import exceptions as google_exceptions
    ResourceExhausted = google_exceptions.ResourceExhausted
    # Errores transitorios del servicio: reintentar con backoff corto
    TRANSIENT = (
        google_exceptions.ServiceUnavailable,   # 503
        google_exceptions.InternalServerError,  # 500
        google_exceptions.DeadlineExceeded,     # 504 / timeout
        google_exceptions.GatewayTimeout,       # 504
    )
except Exception:  # por si cambia el paquete
    class ResourceExhausted(Exception):
        pass
    TRANSIENT = ()


# Pausa entre clientes en modo masivo. El free-tier de Gemini limita a 5 req/min
# (y tiene además un tope DIARIO). Espaciamos varios minutos para no saturar el
# límite por minuto y repartir la carga. Ajustables vía variables de entorno.
# Cada cliente del Estratega hace 1 llamada; el Espía hace 3.
PACING_STRATEGIST = int(os.getenv('PACING_STRATEGIST', '180'))  # 3 min entre clientes
PACING_SPY = int(os.getenv('PACING_SPY', '240'))               # 4 min entre clientes


def _parse_wait_seconds(error, default=20):
    """Extrae los segundos de espera sugeridos por la API del mensaje de error."""
    msg = str(error)
    m = re.search(r'retry in (\d+(?:\.\d+)?)\s*s', msg)
    if m:
        return int(float(m.group(1))) + 3
    m = re.search(r'seconds:\s*(\d+)', msg)
    if m:
        return int(m.group(1)) + 3
    return default


def generate_with_retry(model, user_prompt, generation_config,
                        stream=True, max_retries=5, on_progress=None):
    """
    Llama a Gemini reintentando ante ResourceExhausted (429 rate-limit).

    - model: instancia de genai.GenerativeModel ya configurada
    - on_progress(total_chars): callback opcional para feedback visual (dots)
    - Devuelve el texto completo (str).
    - Lanza la última excepción si se agotan los reintentos o ante otros errores.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = model.generate_content(
                user_prompt,
                generation_config=generation_config,
                stream=stream
            )
            raw = ''
            if stream:
                for chunk in resp:
                    if chunk.text:
                        raw += chunk.text
                        if on_progress:
                            on_progress(len(raw))
            else:
                raw = resp.text or ''
            return raw

        except ResourceExhausted as e:
            wait = _parse_wait_seconds(e)
            if attempt < max_retries:
                print(f"\n    ⏳ Rate limit de Gemini. Esperando {wait}s "
                      f"(intento {attempt}/{max_retries})...", flush=True)
                time.sleep(wait)
            else:
                print(f"\n    ✗ Rate limit persistente tras {max_retries} intentos.", flush=True)
                raise

        except TRANSIENT as e:
            wait = min(5 * attempt, 30)  # backoff corto para errores transitorios (503/500/timeout)
            if attempt < max_retries:
                print(f"\n    ⏳ Servicio Gemini no disponible ({type(e).__name__}). "
                      f"Reintentando en {wait}s (intento {attempt}/{max_retries})...", flush=True)
                time.sleep(wait)
            else:
                print(f"\n    ✗ Servicio Gemini no disponible tras {max_retries} intentos.", flush=True)
                raise