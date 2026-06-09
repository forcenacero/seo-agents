"""
Agente Performance - Lee Search Console y guarda:
- Métricas agregadas por día (metrics_daily)
- Métricas por URL y día (metrics_pages_daily)
- Métricas por keyword y día (metrics_keywords_daily)
- Visibilidad en IA por consultas conversacionales (ai_visibility_daily)
"""
import os
import sys
import json
from datetime import date, timedelta
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()


# === Conexión a Search Console ===
def get_gsc_service():
    """Carga credenciales desde archivo (local) o desde variable de entorno (GitHub Actions)."""
    json_content = os.getenv('GSC_SERVICE_ACCOUNT_JSON')
    
    if json_content:
        info = json.loads(json_content)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    else:
        credentials = service_account.Credentials.from_service_account_file(
            os.getenv('GSC_CREDENTIALS_PATH'),
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    
    return build('searchconsole', 'v1', credentials=credentials)


# === Llamada genérica a Search Analytics ===
def fetch_gsc(service, gsc_property, start_date, end_date, dimensions, row_limit=1000):
    """Lee datos de Search Analytics con las dimensiones que se le pidan."""
    response = service.searchanalytics().query(
        siteUrl=gsc_property,
        body={
            'startDate': start_date.isoformat(),
            'endDate': end_date.isoformat(),
            'dimensions': dimensions,
            'rowLimit': row_limit
        }
    ).execute()
    return response.get('rows', [])


# === Log de ejecución ===
def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    """Registra la ejecución en agent_runs vía endpoint."""
    db.execute("""
        INSERT INTO agent_runs 
            (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'performance', ?, NOW(), ?, ?, ?)
    """, [
        client_id,
        status,
        json.dumps(input_summary) if input_summary else None,
        json.dumps(output_summary) if output_summary else None,
        error
    ])


# === Persistencia: totales diarios ===
def save_daily_totals(client_id, service, gsc_property, start_date, end_date):
    """Totales por día (metrics_daily)."""
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date'])
    saved = 0
    for row in rows:
        db.execute("""
            INSERT INTO metrics_daily 
                (client_id, date, total_clicks, total_impressions, avg_position)
            VALUES (?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                total_clicks = VALUES(total_clicks),
                total_impressions = VALUES(total_impressions),
                avg_position = VALUES(avg_position)
        """, [
            client_id,
            row['keys'][0],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('position', 0), 2)
        ])
        saved += 1
    return saved


# === Persistencia: métricas por URL ===
def save_pages(client_id, service, gsc_property, start_date, end_date):
    """Métricas por URL y día (metrics_pages_daily) - en lote."""
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date', 'page'])
    if not rows:
        return 0
    
    batch = []
    for row in rows:
        batch.append([
            client_id,
            row['keys'][0],
            row['keys'][1][:1000],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('ctr', 0), 4),
            round(row.get('position', 0), 2)
        ])
    
    db.execute_many("""
        INSERT INTO metrics_pages_daily 
            (client_id, date, url, clicks, impressions, ctr, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            clicks = VALUES(clicks),
            impressions = VALUES(impressions),
            ctr = VALUES(ctr),
            position = VALUES(position)
    """, batch)
    return len(batch)


# === Persistencia: métricas por keyword ===
def save_keywords(client_id, service, gsc_property, start_date, end_date):
    """Métricas por keyword y día (metrics_keywords_daily) - en lote."""
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date', 'query'])
    if not rows:
        return 0
    
    batch = []
    for row in rows:
        batch.append([
            client_id,
            row['keys'][0],
            row['keys'][1][:500],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('ctr', 0), 4),
            round(row.get('position', 0), 2)
        ])
    
    db.execute_many("""
        INSERT INTO metrics_keywords_daily 
            (client_id, date, keyword, clicks, impressions, ctr, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            clicks = VALUES(clicks),
            impressions = VALUES(impressions),
            ctr = VALUES(ctr),
            position = VALUES(position)
    """, batch)
    return len(batch)


# === Persistencia: visibilidad en IA ===
def classify_query_source(query):
    """
    Clasificación heurística de queries con patrones típicos de IA.
    Devuelve la fuente probable o None si parece búsqueda tradicional.
    """
    word_count = len(query.split())
    
    # Patrones conversacionales típicos de prompts a IA
    conversational_starters = [
        'cómo puedo', 'cómo se', 'qué es', 'qué son', 'por qué',
        'cuál es la diferencia', 'cuáles son', 'me puedes', 'puedes explicar',
        'explícame', 'dame', 'recomiéndame', 'ayúdame', 'necesito',
        'comment', 'pourquoi', 'quelle est', 'quel est',
        'how can i', 'how do i', 'what is', 'what are', 'why does',
        'can you', 'please', 'tell me', 'explain'
    ]
    
    starts_conversational = any(query.startswith(s) for s in conversational_starters)
    
    # Heurística: queries de 6+ palabras conversacionales = candidatas a IA
    if word_count >= 6 and starts_conversational:
        return 'ai_likely'
    
    # Queries muy largas (10+ palabras) suelen venir de IAs
    if word_count >= 10:
        return 'ai_likely'
    
    return None


def save_ai_visibility(client_id, service, gsc_property, start_date, end_date):
    """
    Detecta tráfico procedente de IAs analizando las queries conversacionales.
    Search Console no expone referrer, pero las queries de IA tienen un patrón
    muy distinto al de las búsquedas tradicionales.
    """
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date', 'query'], row_limit=5000)
    if not rows:
        return 0
    
    # Agregamos por (fecha, source)
    by_date_source = {}
    
    for row in rows:
        metric_date = row['keys'][0]
        query = row['keys'][1].lower()
        clicks = int(row.get('clicks', 0))
        impressions = int(row.get('impressions', 0))
        
        source = classify_query_source(query)
        if not source:
            continue
        
        key = (metric_date, source)
        if key not in by_date_source:
            by_date_source[key] = {
                'clicks': 0,
                'impressions': 0,
                'top_query': query,
                'top_clicks': clicks
            }
        
        by_date_source[key]['clicks'] += clicks
        by_date_source[key]['impressions'] += impressions
        if clicks > by_date_source[key]['top_clicks']:
            by_date_source[key]['top_query'] = query
            by_date_source[key]['top_clicks'] = clicks
    
    if not by_date_source:
        return 0
    
    batch = []
    for (metric_date, source), data in by_date_source.items():
        batch.append([
            client_id,
            metric_date,
            source,
            data['clicks'],
            data['impressions'],
            data['top_query'][:500]
        ])
    
    db.execute_many("""
        INSERT INTO ai_visibility_daily 
            (client_id, date, source, clicks, impressions, top_query)
        VALUES (?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            clicks = VALUES(clicks),
            impressions = VALUES(impressions),
            top_query = VALUES(top_query)
    """, batch)
    
    return len(batch)


# === Ejecución por cliente ===
def run_for_client(client):
    """Ejecuta el agente para un cliente concreto."""
    print(f"\n→ Procesando: {client['name']} ({client['domain']})")
    
    try:
        service = get_gsc_service()
        
        # Pedimos los últimos 7 días (Search Console suele tener delay de 2-3 días)
        end_date = date.today() - timedelta(days=2)
        start_date = end_date - timedelta(days=6)
        
        totals = save_daily_totals(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Totales diarios: {totals} filas")
        
        pages = save_pages(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Métricas por URL: {pages} filas")
        
        keywords = save_keywords(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Métricas por keyword: {keywords} filas")
        
        ai = save_ai_visibility(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Visibilidad IA: {ai} entradas")
        
        save_run(
            client['id'],
            'success',
            input_summary={'range': f"{start_date} to {end_date}"},
            output_summary={
                'totals': totals,
                'pages': pages,
                'keywords': keywords,
                'ai_visibility': ai
            }
        )
    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        try:
            save_run(client['id'], 'error', error=str(e))
        except Exception as inner:
            print(f"  ⚠ No se pudo registrar el error: {inner}")
        raise


# === Loop principal ===
def main():
    """Recorre todos los clientes activos."""
    clients = db.query("SELECT id, domain, name, gsc_property FROM clients WHERE active = 1")
    print(f"Encontrados {len(clients)} clientes activos")
    
    for client in clients:
        if not client['gsc_property']:
            print(f"  ⚠ Cliente {client['name']} sin gsc_property, saltando")
            continue
        try:
            run_for_client(client)
        except Exception:
            continue  # Un cliente que falle no rompe los demás


if __name__ == '__main__':
    main()