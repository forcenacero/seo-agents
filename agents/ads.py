"""
Agente Google Ads (Fase 2).

Descarga los resultados de Google Ads de cada cliente que tenga
`clients.google_ads_customer_id` y los guarda por mes:
  - ads_metrics_monthly    (totales por cliente/mes: coste, clics, impresiones, conversiones, valor)
  - ads_campaigns_monthly  (detalle por campaña)

Usa la librería oficial google-ads, configurada por variables de entorno (secrets):
  GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
  GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID (la MCC), GOOGLE_ADS_USE_PROTO_PLUS=True

Uso:
  python agents/ads.py [client_id] [--months N]
"""
import os
import sys
import json
from datetime import date
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()

MONTHS = int(os.getenv('ADS_MONTHS', '6'))   # cuántos meses hacia atrás traer


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'ads', ?, NOW(), ?, ?, ?)
    """, [client_id, status,
          json.dumps(input_summary) if input_summary else None,
          json.dumps(output_summary) if output_summary else None, error])


def get_client():
    """Crea el GoogleAdsClient desde las variables de entorno."""
    from google.ads.googleads.client import GoogleAdsClient
    if os.getenv('GOOGLE_ADS_USE_PROTO_PLUS') is None:
        os.environ['GOOGLE_ADS_USE_PROTO_PLUS'] = 'True'
    return GoogleAdsClient.load_from_env()


def clean_cid(cid):
    return ''.join(ch for ch in str(cid or '') if ch.isdigit())


def fetch_currency(client, ga, cid):
    try:
        rows = ga.search(customer_id=cid, query="SELECT customer.currency_code FROM customer LIMIT 1")
        for r in rows:
            return r.customer.currency_code
    except Exception:
        pass
    return None


def run_for_client(client, months=MONTHS):
    cid_raw = client.get('google_ads_customer_id')
    cid = clean_cid(cid_raw)
    name = client['name']
    if not cid:
        return {'skipped': True}
    print(f"\n{'='*60}\n  Google Ads · {name} (cuenta {cid})\n{'='*60}")

    from google.ads.googleads.errors import GoogleAdsException
    gclient = get_client()
    ga = gclient.get_service("GoogleAdsService")

    start = (date.today().replace(day=1) - relativedelta(months=months - 1)).isoformat()
    end = date.today().isoformat()
    currency = fetch_currency(gclient, ga, cid)

    query = f"""
        SELECT campaign.id, campaign.name, campaign.status, segments.month,
               metrics.cost_micros, metrics.clicks, metrics.impressions,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    # agregado por (mes) y por (mes, campaña)
    by_month = {}
    by_camp = {}
    try:
        stream = ga.search_stream(customer_id=cid, query=query)
        for batch in stream:
            for row in batch.results:
                month = str(row.segments.month)[:7]          # 'YYYY-MM'
                cost = row.metrics.cost_micros / 1_000_000.0
                clk = row.metrics.clicks
                imp = row.metrics.impressions
                conv = row.metrics.conversions
                cval = row.metrics.conversions_value
                m = by_month.setdefault(month, {'cost': 0, 'clicks': 0, 'impressions': 0, 'conversions': 0, 'conv_value': 0})
                m['cost'] += cost; m['clicks'] += clk; m['impressions'] += imp
                m['conversions'] += conv; m['conv_value'] += cval
                ck = (month, str(row.campaign.id))
                c = by_camp.setdefault(ck, {'name': row.campaign.name, 'status': str(row.campaign.status.name),
                                            'cost': 0, 'clicks': 0, 'impressions': 0, 'conversions': 0, 'conv_value': 0})
                c['cost'] += cost; c['clicks'] += clk; c['impressions'] += imp
                c['conversions'] += conv; c['conv_value'] += cval
    except GoogleAdsException as e:
        msg = '; '.join(err.message for err in e.failure.errors) if e.failure else str(e)
        print(f"  ⚠ Google Ads error: {msg}")
        save_run(client['id'], 'error', {'customer_id': cid}, None, msg[:300])
        return {'error': msg}

    # guardar totales por mes
    for month, m in by_month.items():
        db.execute("""
            INSERT INTO ads_metrics_monthly (client_id, month, currency, cost, clicks, impressions, conversions, conv_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NOW())
            ON DUPLICATE KEY UPDATE currency=VALUES(currency), cost=VALUES(cost), clicks=VALUES(clicks),
                impressions=VALUES(impressions), conversions=VALUES(conversions), conv_value=VALUES(conv_value), updated_at=NOW()
        """, [client['id'], month, currency, round(m['cost'], 2), int(m['clicks']), int(m['impressions']),
              round(m['conversions'], 2), round(m['conv_value'], 2)])
    # guardar por campaña
    for (month, camp_id), c in by_camp.items():
        db.execute("""
            INSERT INTO ads_campaigns_monthly (client_id, month, campaign_id, campaign_name, status, cost, clicks, impressions, conversions, conv_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE campaign_name=VALUES(campaign_name), status=VALUES(status), cost=VALUES(cost),
                clicks=VALUES(clicks), impressions=VALUES(impressions), conversions=VALUES(conversions), conv_value=VALUES(conv_value)
        """, [client['id'], month, camp_id, c['name'][:255], c['status'], round(c['cost'], 2), int(c['clicks']),
              int(c['impressions']), round(c['conversions'], 2), round(c['conv_value'], 2)])

    # Conversiones por tipo de acción (teléfono, formulario…) por mes
    try:
        cquery = f"""
            SELECT segments.conversion_action_name, segments.conversion_action_category, segments.month,
                   metrics.conversions, metrics.conversions_value
            FROM customer
            WHERE segments.date BETWEEN '{start}' AND '{end}'
        """
        conv_by = {}   # (month, name) -> {cat, conv, val}
        for batch in ga.search_stream(customer_id=cid, query=cquery):
            for row in batch.results:
                month = str(row.segments.month)[:7]
                name = row.segments.conversion_action_name or '(sin nombre)'
                cat = getattr(row.segments.conversion_action_category, 'name', str(row.segments.conversion_action_category))
                k = (month, name)
                e = conv_by.setdefault(k, {'cat': cat, 'conv': 0, 'val': 0})
                e['conv'] += row.metrics.conversions; e['val'] += row.metrics.conversions_value
        for (month, name), e in conv_by.items():
            db.execute("""
                INSERT INTO ads_conversions_monthly (client_id, month, action_name, category, conversions, conv_value)
                VALUES (?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE category=VALUES(category), conversions=VALUES(conversions), conv_value=VALUES(conv_value)
            """, [client['id'], month, name[:255], e['cat'], round(e['conv'], 2), round(e['val'], 2)])
        print(f"  · conversiones por tipo: {len(conv_by)} filas")
    except Exception as e:
        print(f"  ⚠ conversiones por tipo no disponibles: {e}")

    last = max(by_month) if by_month else None
    lm = by_month.get(last, {})
    print(f"  → {len(by_month)} meses · {len(by_camp)} campañas · último mes {last}: "
          f"{lm.get('cost',0):.0f}{currency or ''} · {int(lm.get('conversions',0))} conv.")
    save_run(client['id'], 'success', {'customer_id': cid, 'months': months},
             {'months': len(by_month), 'campaigns': len(by_camp), 'currency': currency})
    return {'months': len(by_month), 'campaigns': len(by_camp)}


def main():
    args = [a for a in sys.argv[1:]]
    months = MONTHS
    if '--months' in args:
        i = args.index('--months'); months = int(args[i + 1]); del args[i:i + 2]
    client_id = args[0] if args else None

    if client_id:
        clients = db.query("SELECT id, name, google_ads_customer_id FROM clients WHERE id = ?", [client_id])
    else:
        clients = db.query("SELECT id, name, google_ads_customer_id FROM clients WHERE active = 1 AND google_ads_customer_id IS NOT NULL AND google_ads_customer_id <> ''")

    print(f"Google Ads · {len(clients)} cliente(s) con cuenta · {months} meses")
    for c in clients:
        try:
            run_for_client(c, months)
        except Exception as e:
            print(f"  ⚠ {c['name']}: {e}")
            save_run(c['id'], 'error', None, None, str(e)[:300])


if __name__ == '__main__':
    main()
