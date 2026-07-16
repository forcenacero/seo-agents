"""
Utilidades de Google Search Console compartidas por los agentes.

Permite obtener las búsquedas REALES (queries) por las que una URL concreta
recibe impresiones/clics, para alimentar la generación de meta/title con datos
reales en vez de solo con el contenido de la página.
"""
import os
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']


def get_gsc_service():
    """Servicio de Search Console. Devuelve None si no hay credenciales."""
    json_content = os.getenv('GSC_SERVICE_ACCOUNT_JSON')
    if json_content:
        info = json.loads(json_content)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path = os.getenv('GSC_CREDENTIALS_PATH')
        if not path or not os.path.exists(path):
            return None
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return build('searchconsole', 'v1', credentials=creds)


def fetch_page_queries(service, gsc_property, page_url, start_date, end_date, limit=12):
    """
    Top queries reales de una URL concreta (dimensión query filtrada por página).
    Devuelve lista de dicts: {query, clicks, impressions, position}, ordenada por impresiones.
    Robusta: si la URL no matchea exacto, prueba con y sin barra final.
    """
    variants = [page_url]
    if page_url.endswith('/'):
        variants.append(page_url[:-1])
    else:
        variants.append(page_url + '/')

    for url in variants:
        try:
            resp = service.searchanalytics().query(
                siteUrl=gsc_property,
                body={
                    'startDate': start_date.isoformat(),
                    'endDate': end_date.isoformat(),
                    'dimensions': ['query'],
                    'dimensionFilterGroups': [{
                        'filters': [{'dimension': 'page', 'operator': 'equals', 'expression': url}]
                    }],
                    'rowLimit': limit,
                }
            ).execute()
        except Exception:
            return []
        rows = resp.get('rows', [])
        if rows:
            return [{
                'query': r['keys'][0],
                'clicks': int(r.get('clicks', 0)),
                'impressions': int(r.get('impressions', 0)),
                'position': round(float(r.get('position', 0)), 1),
            } for r in rows]
    return []