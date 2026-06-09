"""
Agente Performance - Lee Search Console y guarda:
- Métricas agregadas por día (metrics_daily)
- Métricas por URL y día (metrics_pages_daily)
- Métricas por keyword y día (metrics_keywords_daily)
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


def get_gsc_service():
    json_content = os.getenv('GSC_SERVICE_ACCOUNT_JSON')
    if json_content:
        info = json.loads(json_content)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    else:
        credentials = service_account.Credentials.from_service_account_file(
            os.getenv('GSC_CREDENTIALS_PATH'),
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    return build('searchconsole', 'v1', credentials=credentials)


def fetch_gsc(service, gsc_property, start_date, end_date, dimensions, row_limit=1000):
    """Llamada genérica a Search Analytics."""
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


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs 
            (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'performance', ?, NOW(), ?, ?, ?)
    """, [
        client_id, status,
        json.dumps(input_summary) if input_summary else None,
        json.dumps(output_summary) if output_summary else None,
        error
    ])


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
            client_id, row['keys'][0],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('position', 0), 2)
        ])
        saved += 1
    return saved


def save_pages(client_id, service, gsc_property, start_date, end_date):
    """Métricas por URL y día (metrics_pages_daily)."""
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date', 'page'])
    saved = 0
    for row in rows:
        metric_date, url = row['keys'][0], row['keys'][1]
        db.execute("""
            INSERT INTO metrics_pages_daily 
                (client_id, date, url, clicks, impressions, ctr, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                clicks = VALUES(clicks),
                impressions = VALUES(impressions),
                ctr = VALUES(ctr),
                position = VALUES(position)
        """, [
            client_id, metric_date, url[:1000],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('ctr', 0), 4),
            round(row.get('position', 0), 2)
        ])
        saved += 1
    return saved


def save_keywords(client_id, service, gsc_property, start_date, end_date):
    """Métricas por keyword y día (metrics_keywords_daily)."""
    rows = fetch_gsc(service, gsc_property, start_date, end_date, ['date', 'query'])
    saved = 0
    for row in rows:
        metric_date, keyword = row['keys'][0], row['keys'][1]
        db.execute("""
            INSERT INTO metrics_keywords_daily 
                (client_id, date, keyword, clicks, impressions, ctr, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                clicks = VALUES(clicks),
                impressions = VALUES(impressions),
                ctr = VALUES(ctr),
                position = VALUES(position)
        """, [
            client_id, metric_date, keyword[:500],
            int(row.get('clicks', 0)),
            int(row.get('impressions', 0)),
            round(row.get('ctr', 0), 4),
            round(row.get('position', 0), 2)
        ])
        saved += 1
    return saved


def run_for_client(client):
    print(f"\n→ Procesando: {client['name']} ({client['domain']})")
    
    try:
        service = get_gsc_service()
        end_date = date.today() - timedelta(days=2)
        start_date = end_date - timedelta(days=6)
        
        totals = save_daily_totals(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Totales diarios: {totals} filas")
        
        pages = save_pages(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Métricas por URL: {pages} filas")
        
        keywords = save_keywords(client['id'], service, client['gsc_property'], start_date, end_date)
        print(f"  ✓ Métricas por keyword: {keywords} filas")
        
        save_run(
            client['id'], 'success',
            input_summary={'range': f"{start_date} to {end_date}"},
            output_summary={'totals': totals, 'pages': pages, 'keywords': keywords}
        )
    except Exception as e:
        print(f"  ✗ Error: {e}")
        try:
            save_run(client['id'], 'error', error=str(e))
        except Exception as inner:
            print(f"  ⚠ No se pudo registrar el error: {inner}")
        raise


def main():
    clients = db.query("SELECT id, domain, name, gsc_property FROM clients WHERE active = 1")
    print(f"Encontrados {len(clients)} clientes activos")
    
    for client in clients:
        if not client['gsc_property']:
            print(f"  ⚠ Cliente {client['name']} sin gsc_property, saltando")
            continue
        try:
            run_for_client(client)
        except Exception:
            continue


if __name__ == '__main__':
    main()