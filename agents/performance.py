"""
Agente Performance - Lee Search Console y guarda métricas diarias.
Habla con la BD a través del endpoint PHP en el hosting.
Diseñado para ejecutarse una vez al día.
"""
import os
import sys
import json
from datetime import date, timedelta
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Permite importar db_client.py desde la misma carpeta agents/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()


# === Conexión a Search Console ===
def get_gsc_service():
    """Carga credenciales desde archivo (local) o desde variable de entorno (GitHub Actions)."""
    json_content = os.getenv('GSC_SERVICE_ACCOUNT_JSON')
    
    if json_content:
        # En GitHub Actions: el JSON viene como string en una variable
        info = json.loads(json_content)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    else:
        # En local: leemos el archivo
        credentials = service_account.Credentials.from_service_account_file(
            os.getenv('GSC_CREDENTIALS_PATH'),
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )
    
    return build('searchconsole', 'v1', credentials=credentials)


# === Lógica del agente ===
def fetch_gsc_metrics(service, gsc_property, start_date, end_date):
    """Lee métricas agregadas por día para un rango."""
    response = service.searchanalytics().query(
        siteUrl=gsc_property,
        body={
            'startDate': start_date.isoformat(),
            'endDate': end_date.isoformat(),
            'dimensions': ['date'],
            'rowLimit': 1000
        }
    ).execute()
    return response.get('rows', [])


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


def run_for_client(client):
    """Ejecuta el agente para un cliente concreto."""
    print(f"\n→ Procesando: {client['name']} ({client['domain']})")
    rows_saved = 0
    
    try:
        service = get_gsc_service()
        
        # Pedimos los últimos 7 días (Search Console suele tener delay de 2-3 días)
        end_date = date.today() - timedelta(days=2)
        start_date = end_date - timedelta(days=6)
        
        rows = fetch_gsc_metrics(service, client['gsc_property'], start_date, end_date)
        print(f"  GSC devolvió {len(rows)} días de datos")
        
        # Guardamos cada día en metrics_daily
        for row in rows:
            metric_date = row['keys'][0]  # formato 'YYYY-MM-DD'
            db.execute("""
                INSERT INTO metrics_daily 
                    (client_id, date, total_clicks, total_impressions, avg_position)
                VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    total_clicks = VALUES(total_clicks),
                    total_impressions = VALUES(total_impressions),
                    avg_position = VALUES(avg_position)
            """, [
                client['id'],
                metric_date,
                int(row.get('clicks', 0)),
                int(row.get('impressions', 0)),
                round(row.get('position', 0), 2)
            ])
            rows_saved += 1
        
        save_run(
            client['id'],
            'success',
            input_summary={'range': f"{start_date} to {end_date}"},
            output_summary={'days_saved': rows_saved}
        )
        
        print(f"  ✓ Guardadas {rows_saved} filas en metrics_daily")
    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        try:
            save_run(client['id'], 'error', error=str(e))
        except Exception as inner:
            print(f"  ⚠ No se pudo registrar el error en agent_runs: {inner}")
        raise


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