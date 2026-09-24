"""
Agente Google Analytics 4 (GA4).

Descarga sesiones y métricas genéricas de tráfico por MES para cada cliente que
tenga `clients.ga4_property_id`, y las guarda en `ga4_metrics_monthly`:
  - sessions, total_users, new_users, engaged_sessions, screen_page_views,
    avg_session_duration, conversions
  - organic_sessions / paid_sessions (desglose por canal por defecto)

Usa la Analytics Data API (google-analytics-data) con la MISMA cuenta de servicio
que Search Console (secret GSC_SERVICE_ACCOUNT_JSON), pero con el scope de Analytics.
La cuenta de servicio debe estar añadida como Lector en cada propiedad GA4.

Uso:
  python agents/ga4.py [client_id] [--months N]
"""
import os
import sys
import json
from datetime import date
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()

SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']
MONTHS = int(os.getenv('GA4_MONTHS', '13'))   # meses hacia atrás (13 → año actual + mismo mes año anterior)

# Métricas genéricas de tráfico. 'conversions' puede no existir en propiedades nuevas
# (migradas a keyEvents); se pide aparte con fallback para no romper el resto.
CORE_METRICS = ['sessions', 'totalUsers', 'newUsers', 'engagedSessions',
                'screenPageViews', 'averageSessionDuration']


def get_ga4_client():
    json_content = os.getenv('GSC_SERVICE_ACCOUNT_JSON')
    if json_content:
        info = json.loads(json_content)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path = os.getenv('GSC_CREDENTIALS_PATH')
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return BetaAnalyticsDataClient(credentials=creds)


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'ga4', ?, NOW(), ?, ?, ?)
    """, [client_id, status,
          json.dumps(input_summary) if input_summary else None,
          json.dumps(output_summary) if output_summary else None, error])


def ym(raw):
    """'202609' → '2026-09'."""
    return raw[:4] + '-' + raw[4:6] if raw and len(raw) >= 6 else raw


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def is_paid(channel):
    ch = (channel or '').lower()
    return ch.startswith('paid') or ch in ('display', 'cross-network')


def run_for_client(client, months=MONTHS):
    prop = str(client.get('ga4_property_id') or '').strip().replace('properties/', '')
    if not prop:
        return
    ga = get_ga4_client()
    property_id = f"properties/{prop}"
    start = (date.today().replace(day=1) - relativedelta(months=months - 1)).isoformat()
    end = date.today().isoformat()
    date_range = [DateRange(start_date=start, end_date=end)]

    # ── Consulta 1: métricas generales por mes ──
    metrics = list(CORE_METRICS)
    conv_ok = True
    try:
        req = RunReportRequest(
            property=property_id, date_ranges=date_range,
            dimensions=[Dimension(name='yearMonth')],
            metrics=[Metric(name=m) for m in metrics + ['conversions']],
        )
        resp = ga.run_report(req)
        metrics = metrics + ['conversions']
    except Exception:
        conv_ok = False
        req = RunReportRequest(
            property=property_id, date_ranges=date_range,
            dimensions=[Dimension(name='yearMonth')],
            metrics=[Metric(name=m) for m in metrics],
        )
        resp = ga.run_report(req)

    monthly = {}
    for row in resp.rows:
        m = ym(row.dimension_values[0].value)
        vals = {metrics[i]: _num(row.metric_values[i].value) for i in range(len(metrics))}
        monthly[m] = {
            'sessions': int(vals.get('sessions', 0)),
            'total_users': int(vals.get('totalUsers', 0)),
            'new_users': int(vals.get('newUsers', 0)),
            'engaged_sessions': int(vals.get('engagedSessions', 0)),
            'screen_page_views': int(vals.get('screenPageViews', 0)),
            'avg_session_duration': round(vals.get('averageSessionDuration', 0), 2),
            'conversions': round(vals.get('conversions', 0), 2) if conv_ok else 0,
            'organic_sessions': 0, 'paid_sessions': 0,
        }

    # ── Consulta 2: sesiones por canal (organic/paid) ──
    try:
        req2 = RunReportRequest(
            property=property_id, date_ranges=date_range,
            dimensions=[Dimension(name='yearMonth'), Dimension(name='sessionDefaultChannelGroup')],
            metrics=[Metric(name='sessions')],
        )
        resp2 = ga.run_report(req2)
        for row in resp2.rows:
            m = ym(row.dimension_values[0].value)
            channel = row.dimension_values[1].value
            s = int(_num(row.metric_values[0].value))
            if m not in monthly:
                continue
            if channel == 'Organic Search':
                monthly[m]['organic_sessions'] += s
            elif is_paid(channel):
                monthly[m]['paid_sessions'] += s
    except Exception as e:
        print(f"    (sin desglose por canal: {e})")

    if not monthly:
        save_run(client['id'], 'success', {'property': prop}, {'months': 0}, None)
        print(f"  · {client['name']}: sin filas GA4")
        return

    rows = []
    for m, d in monthly.items():
        rows.append([client['id'], m, d['sessions'], d['total_users'], d['new_users'],
                     d['engaged_sessions'], d['screen_page_views'], d['avg_session_duration'],
                     d['conversions'], d['organic_sessions'], d['paid_sessions']])
    db.execute_many("""
        INSERT INTO ga4_metrics_monthly
            (client_id, month, sessions, total_users, new_users, engaged_sessions,
             screen_page_views, avg_session_duration, conversions, organic_sessions, paid_sessions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            sessions=VALUES(sessions), total_users=VALUES(total_users), new_users=VALUES(new_users),
            engaged_sessions=VALUES(engaged_sessions), screen_page_views=VALUES(screen_page_views),
            avg_session_duration=VALUES(avg_session_duration), conversions=VALUES(conversions),
            organic_sessions=VALUES(organic_sessions), paid_sessions=VALUES(paid_sessions)
    """, rows)

    last = sorted(monthly.keys())[-1]
    print(f"  ✓ {client['name']}: {len(rows)} meses · último {last} = {monthly[last]['sessions']} sesiones")
    save_run(client['id'], 'success', {'property': prop, 'months': months},
             {'rows': len(rows), 'last_month': last, 'last_sessions': monthly[last]['sessions']}, None)


def main():
    args = [a for a in sys.argv[1:]]
    months = MONTHS
    if '--months' in args:
        i = args.index('--months'); months = int(args[i + 1]); del args[i:i + 2]
    client_id = args[0] if args else None

    if client_id:
        clients = db.query("SELECT id, name, ga4_property_id FROM clients WHERE id = ?", [client_id])
    else:
        clients = db.query("SELECT id, name, ga4_property_id FROM clients WHERE active = 1 AND ga4_property_id IS NOT NULL AND ga4_property_id <> ''")

    print(f"GA4 · {len(clients)} cliente(s) con propiedad · {months} meses")
    for c in clients:
        try:
            run_for_client(c, months)
        except Exception as e:
            print(f"  ⚠ {c['name']}: {e}")
            save_run(c['id'], 'error', None, None, str(e)[:300])


if __name__ == '__main__':
    main()
