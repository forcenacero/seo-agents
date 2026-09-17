"""
Agente PRESENCIA EN IA (GEO real).

Para las consultas más importantes de cada cliente, pregunta a Gemini con
*grounding* de Google Search y mira si el dominio del cliente aparece CITADO en
la respuesta de la IA (que es lo que alimenta los AI Overviews de Google).

Guarda por mes en `ai_geo_checks`:
  - mentioned  = si la IA cita/nombra al cliente (por cita o por marca en el texto)
  - rank       = posición del cliente entre las fuentes citadas (1 = primera)
  - cited_domains = dominios que la IA cita (competidores + el propio) en orden
  - answer_excerpt = extracto de la respuesta de la IA

Esto convierte la "presencia en IA" de una heurística (ai_likely) en una señal real.

Uso:
  python agents/ai_presence.py [client_id] [--limit N]
"""
import os
import sys
import json
import re
import time
import requests
from datetime import date
from urllib.parse import urlparse
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()

API_KEY = os.getenv('GEMINI_API_KEY')
# Modelo con soporte de grounding (Google Search). Configurable por si cambia el catálogo.
MODEL = os.getenv('GEMINI_GROUNDING_MODEL', 'gemini-3.6-flash')
MAX_QUERIES = int(os.getenv('AI_PRESENCE_MAX_QUERIES', '12'))
MONTH = date.today().strftime('%Y-%m')
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'ai_presence', ?, NOW(), ?, ?, ?)
    """, [client_id, status,
          json.dumps(input_summary) if input_summary else None,
          json.dumps(output_summary) if output_summary else None, error])


def registrable(host):
    """Dominio normalizado sin esquema ni www ni ruta."""
    host = (host or '').strip().lower()
    if '://' in host:
        host = urlparse(host).netloc or host
    host = host.split('/')[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$')


def cited_domains_from(candidate):
    """Lista ordenada de dominios citados (de groundingChunks[].web.title)."""
    gm = candidate.get('groundingMetadata', {}) or {}
    out = []
    for ch in gm.get('groundingChunks', []) or []:
        title = (ch.get('web', {}) or {}).get('title', '').strip().lower()
        dom = registrable(title)
        if DOMAIN_RE.match(dom) and dom not in out:
            out.append(dom)
    return out


def ground(query):
    """Devuelve (answer_text, cited_domains) o (None, None) si falla."""
    # Instrucción que FUERZA la búsqueda + citas (si no, Gemini a veces responde de memoria
    # sin buscar y el "no aparece" no sería fiable).
    prompt = (f"{query}\n\nBusca información actualizada en internet y responde en español "
              f"citando las webs y empresas más relevantes para esta consulta.")
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    try:
        r = requests.post(ENDPOINT, json=body, timeout=70, headers={'Content-Type': 'application/json'})
        if r.status_code != 200:
            print(f"    ⚠ Gemini HTTP {r.status_code}: {r.text[:120]}")
            return None, None
        d = r.json()
        cand = (d.get('candidates') or [{}])[0]
        text = ''.join(p.get('text', '') for p in cand.get('content', {}).get('parts', []))
        return text, cited_domains_from(cand)
    except Exception as e:
        print(f"    ⚠ error grounding: {e}")
        return None, None


def top_queries(client_id):
    """Consultas más importantes del cliente (GSC: por impresiones, últimos 90 días)."""
    rows = db.query("""
        SELECT keyword, SUM(impressions) impr
        FROM metrics_keywords_daily
        WHERE client_id = ? AND date >= (CURRENT_DATE - INTERVAL 90 DAY) AND keyword <> ''
        GROUP BY keyword ORDER BY impr DESC LIMIT ?
    """, [client_id, MAX_QUERIES])
    return [r['keyword'] for r in rows]


def run_for_client(client, limit=MAX_QUERIES):
    cid = client['id']
    cdom = registrable(client.get('domain', ''))
    brand = (client.get('name', '') or '').lower()
    print(f"\n{'='*60}\n  Presencia en IA · {client['name']} ({cdom})\n{'='*60}")
    queries = top_queries(cid)[:limit]
    if not queries:
        print("  ✓ Sin consultas (no hay datos GSC).")
        return {'checked': 0, 'mentioned': 0}

    checked = 0
    mentioned = 0
    for q in queries:
        text, domains = ground(q)
        if text is None:
            continue
        checked += 1
        rank = None
        for i, dom in enumerate(domains):
            if cdom and (cdom in dom or dom in cdom):
                rank = i + 1
                break
        brand_in_text = bool(brand and len(brand) > 2 and brand in (text or '').lower())
        cdom_in_text = bool(cdom and cdom in (text or '').lower())
        is_mentioned = rank is not None or brand_in_text or cdom_in_text
        if is_mentioned:
            mentioned += 1
        print(f"    {'✓' if is_mentioned else '·'} {q[:48]:48} {'#'+str(rank) if rank else ('marca' if is_mentioned else 'no aparece')} · {len(domains)} fuentes")
        db.execute("""
            INSERT INTO ai_geo_checks (client_id, month, query, engine, mentioned, `rank`, cited_domains, answer_excerpt, checked_at)
            VALUES (?, ?, ?, 'gemini_grounded', ?, ?, ?, ?, NOW())
            ON DUPLICATE KEY UPDATE
                mentioned = VALUES(mentioned), `rank` = VALUES(`rank`),
                cited_domains = VALUES(cited_domains), answer_excerpt = VALUES(answer_excerpt), checked_at = NOW()
        """, [cid, MONTH, q[:255], 1 if is_mentioned else 0, rank,
              json.dumps(domains, ensure_ascii=False), (text or '')[:800]])
        time.sleep(1.5)   # ritmo suave

    pct = round(mentioned / checked * 100) if checked else 0
    print(f"  → {mentioned}/{checked} consultas te citan ({pct}%)")
    save_run(cid, 'success', {'queries': len(queries)}, {'checked': checked, 'mentioned': mentioned, 'pct': pct})
    return {'checked': checked, 'mentioned': mentioned}


def main():
    args = [a for a in sys.argv[1:]]
    limit = MAX_QUERIES
    if '--limit' in args:
        i = args.index('--limit'); limit = int(args[i + 1]); del args[i:i + 2]
    client_id = args[0] if args else None

    if not API_KEY:
        print("Falta GEMINI_API_KEY"); sys.exit(1)

    if client_id:
        clients = db.query("SELECT id, domain, name FROM clients WHERE id = ?", [client_id])
    else:
        clients = db.query("SELECT id, domain, name FROM clients WHERE active = 1 ORDER BY id")

    print(f"Presencia en IA · modelo {MODEL} · mes {MONTH} · {len(clients)} cliente(s)")
    for c in clients:
        try:
            run_for_client(c, limit)
        except Exception as e:
            print(f"  ⚠ {c['name']}: {e}")
            save_run(c['id'], 'error', None, None, str(e)[:300])


if __name__ == '__main__':
    main()
