"""
Agente Espía - Vigila competidores definidos manualmente.
Crawlea sus sitemaps, detecta páginas nuevas y desaparecidas, extrae datos básicos.
Genera insights con Gemini comparando con datos del cliente.
"""
import os
import sys
import json
import time
import hashlib
import requests
from datetime import date, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()


# === Configuración ===
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SEO-Agents-Spy/1.0'
TIMEOUT = 15
MAX_URLS_PER_COMPETITOR = 100
REQUEST_DELAY = 1.0

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-3.5-flash'


def fetch(url, **kwargs):
    headers = {'User-Agent': USER_AGENT, **kwargs.pop('headers', {})}
    return requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, **kwargs)


# === Descubrimiento de URLs vía sitemap ===
def discover_urls(domain):
    """Lee el sitemap del competidor y devuelve la lista de URLs."""
    candidates = [
        f"https://{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/wp-sitemap.xml",
        f"https://{domain}/sitemap_index.xml.gz"
    ]
    
    for sitemap_url in candidates:
        try:
            r = fetch(sitemap_url)
            if r.status_code == 200 and ('xml' in r.headers.get('Content-Type', '') or '<urlset' in r.text or '<sitemapindex' in r.text):
                return parse_sitemap(sitemap_url, r.text)
        except requests.RequestException:
            continue
    
    return []


def parse_sitemap(sitemap_url, content):
    urls = []
    try:
        content_clean = content
        for ns in ['xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"']:
            content_clean = content_clean.replace(ns, '')
        
        root = ET.fromstring(content_clean)
        
        for sitemap in root.findall('.//sitemap/loc'):
            sub_url = sitemap.text.strip() if sitemap.text else ''
            if sub_url:
                try:
                    r = fetch(sub_url)
                    if r.status_code == 200:
                        urls.extend(parse_sitemap(sub_url, r.text))
                except requests.RequestException:
                    continue
        
        for url in root.findall('.//url/loc'):
            if url.text:
                urls.append(url.text.strip())
    
    except ET.ParseError:
        pass
    
    return urls


def url_hash(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


# === Análisis ligero de una página del competidor ===
def analyze_page(url):
    """Extrae title, meta, H1 y word count de una página del competidor."""
    try:
        r = fetch(url)
        if r.status_code >= 400:
            return None
        if 'text/html' not in r.headers.get('Content-Type', ''):
            return None
        
        soup = BeautifulSoup(r.content, 'lxml')
        
        title = ''
        title_tag = soup.find('title')
        if title_tag and title_tag.text:
            title = title_tag.text.strip()[:500]
        
        meta_desc = ''
        meta = soup.find('meta', attrs={'name': 'description'})
        if meta and meta.get('content'):
            meta_desc = meta.get('content').strip()[:500]
        
        h1 = ''
        h1_tag = soup.find('h1')
        if h1_tag and h1_tag.text:
            h1 = h1_tag.text.strip()[:500]
        
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        word_count = len(text.split())
        
        return {
            'title': title,
            'meta_description': meta_desc,
            'h1': h1,
            'word_count': word_count
        }
    except Exception:
        return None


# === Persistencia ===
def get_existing_urls(competitor_id):
    """URLs ya conocidas para un competidor (las que no han sido eliminadas)."""
    rows = db.query("""
        SELECT url, url_hash 
        FROM competitor_pages 
        WHERE competitor_id = ? AND removed_at IS NULL
    """, [competitor_id])
    return {r['url_hash']: r['url'] for r in rows}


def save_page(competitor_id, url, today, page_data, is_new):
    """Guarda o actualiza una página de un competidor."""
    h = url_hash(url)
    
    if is_new:
        db.execute("""
            INSERT INTO competitor_pages 
                (competitor_id, url, url_hash, title, meta_description, h1, word_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                last_seen = VALUES(last_seen),
                title = VALUES(title),
                meta_description = VALUES(meta_description),
                h1 = VALUES(h1),
                word_count = VALUES(word_count),
                removed_at = NULL
        """, [
            competitor_id, url[:1000], h,
            page_data['title'], page_data['meta_description'], page_data['h1'],
            page_data['word_count'], today, today
        ])
    else:
        db.execute("""
            UPDATE competitor_pages 
            SET last_seen = ?
            WHERE competitor_id = ? AND url_hash = ?
        """, [today, competitor_id, h])


def mark_removed(competitor_id, url_hashes_disappeared, today):
    """Marca como eliminadas las URLs que ya no aparecen en el sitemap."""
    if not url_hashes_disappeared:
        return
    
    for h in url_hashes_disappeared:
        db.execute("""
            UPDATE competitor_pages 
            SET removed_at = ?
            WHERE competitor_id = ? AND url_hash = ? AND removed_at IS NULL
        """, [today, competitor_id, h])


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs 
            (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'spy', ?, NOW(), ?, ?, ?)
    """, [
        client_id, status,
        json.dumps(input_summary) if input_summary else None,
        json.dumps(output_summary) if output_summary else None,
        error
    ])


def save_insights(client_id, insights):
    """Guarda los insights generados por Gemini."""
    for ins in insights:
        db.execute("""
            INSERT INTO competitor_insights 
                (client_id, insight_type, competitor_id, title, description, data_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            client_id,
            ins.get('insight_type', 'tactic_detected'),
            ins.get('competitor_id'),
            ins.get('title', '')[:500],
            ins.get('description', ''),
            json.dumps(ins.get('data', {}))
        ])


# === Crawl de un competidor ===
def spy_on_competitor(competitor):
    """Crawlea el sitemap del competidor y guarda las páginas detectadas."""
    print(f"\n  Espiando: {competitor['name']} ({competitor['domain']})")
    
    urls_found = discover_urls(competitor['domain'])
    if not urls_found:
        print(f"    ⚠ No se pudo acceder al sitemap")
        return {'new': 0, 'existing': 0, 'removed': 0, 'analyzed': 0}
    
    urls_found = list(set(urls_found))[:MAX_URLS_PER_COMPETITOR]
    print(f"    Encontradas {len(urls_found)} URLs en sitemap")
    
    existing = get_existing_urls(competitor['id'])
    found_hashes = {url_hash(u) for u in urls_found}
    
    new_urls = [u for u in urls_found if url_hash(u) not in existing]
    disappeared = set(existing.keys()) - found_hashes
    
    today = date.today().isoformat()
    
    # Analizar páginas nuevas (las existentes solo actualizan last_seen)
    analyzed = 0
    for url in new_urls:
        page_data = analyze_page(url)
        if page_data:
            save_page(competitor['id'], url, today, page_data, is_new=True)
            analyzed += 1
        else:
            save_page(competitor['id'], url, today, {
                'title': '', 'meta_description': '', 'h1': '', 'word_count': 0
            }, is_new=True)
        time.sleep(REQUEST_DELAY)
    
    # Actualizar last_seen para las existentes
    for url in urls_found:
        if url_hash(url) in existing:
            save_page(competitor['id'], url, today, {}, is_new=False)
    
    # Marcar como eliminadas las que ya no aparecen
    mark_removed(competitor['id'], list(disappeared), today)
    
    print(f"    ✓ Nuevas: {len(new_urls)} · Existentes: {len(existing) - len(disappeared)} · Eliminadas: {len(disappeared)}")
    
    return {
        'new': len(new_urls),
        'existing': len(existing) - len(disappeared),
        'removed': len(disappeared),
        'analyzed': analyzed
    }


# === Generación de insights con Gemini ===
def generate_insights(client_id, client_info, competitors_data):
    """Pide a Gemini que analice los datos cruzados y genere insights útiles."""
    if not GEMINI_API_KEY:
        print("  ⚠ Sin GEMINI_API_KEY, saltando generación de insights")
        return []
    
    # Construir contexto
    context = {
        'client': client_info,
        'competitors': competitors_data
    }
    
    system_prompt = """Eres un analista SEO competitivo senior. Tu trabajo es revisar datos de un cliente y sus competidores, e identificar 3-5 insights accionables.

Tipos de insights válidos:
- new_content: el competidor publicó contenido nuevo relevante
- gap_topic: tema que cubren los competidores y el cliente no
- volume_disparity: gran diferencia de volumen de páginas entre cliente y competencia
- tactic_detected: táctica SEO que están usando los competidores

Responde SOLO con JSON válido en este formato:

{
  "insights": [
    {
      "insight_type": "new_content",
      "competitor_index": 0,
      "title": "Competidor X publicó 5 nuevas páginas sobre temas Y",
      "description": "Análisis concreto con números y temas detectados...",
      "data": {"any": "extra info"}
    }
  ]
}

Reglas estrictas:
- Genera entre 3 y 5 insights
- Sé específico con números reales del contexto
- Si no hay datos suficientes para un tipo, no inventes; usa otros tipos
- competitor_index es el índice 0-based del competidor en la lista, o null si el insight no aplica a uno concreto
"""

    user_prompt = f"""Datos del cliente y sus competidores:

{json.dumps(context, indent=2, ensure_ascii=False)}

Genera 3-5 insights accionables en formato JSON."""

    try:
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction=system_prompt
        )
        
        response_stream = model.generate_content(
            user_prompt,
            generation_config={
                'temperature': 0.4,
                'max_output_tokens': 4000,
                'response_mime_type': 'application/json'
            },
            stream=True
        )
        
        print("    Generando insights con Gemini ", end='', flush=True)
        raw_text = ''
        for chunk in response_stream:
            if chunk.text:
                raw_text += chunk.text
                print('.', end='', flush=True)
        print()
        
        result = json.loads(raw_text.strip())
        insights = result.get('insights', [])
        
        # Mapear competitor_index a competitor_id
        for ins in insights:
            idx = ins.get('competitor_index')
            if idx is not None and idx < len(competitors_data):
                ins['competitor_id'] = competitors_data[idx]['id']
            else:
                ins['competitor_id'] = None
        
        return insights
    except Exception as e:
        print(f"    ⚠ Error generando insights: {e}")
        return []


# === Ejecución por cliente ===
def run_for_client(client_id):
    client = db.query_one("SELECT id, name, domain FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return
    
    print(f"\n→ Analizando competencia de: {client['name']}")
    
    # Cargar competidores activos del cliente
    competitors = db.query("""
        SELECT id, domain, name 
        FROM competitors 
        WHERE client_id = ? AND active = 1
    """, [client_id])
    
    if not competitors:
        print(f"  ⚠ Sin competidores definidos para este cliente. Añade competidores en la tabla 'competitors'.")
        save_run(client_id, 'error', error='No competitors defined')
        return
    
    print(f"  Competidores: {len(competitors)}")
    
    # Espionar cada competidor
    all_stats = []
    competitors_data = []
    
    for competitor in competitors:
        try:
            stats = spy_on_competitor(competitor)
            all_stats.append({**stats, 'competitor': competitor['name']})
            
            # Datos para el análisis posterior
            recent_pages = db.query("""
                SELECT url, title, h1, word_count, first_seen
                FROM competitor_pages
                WHERE competitor_id = ? AND removed_at IS NULL
                ORDER BY first_seen DESC
                LIMIT 30
            """, [competitor['id']])
            
            new_this_week = db.query("""
                SELECT url, title, h1, word_count, first_seen
                FROM competitor_pages
                WHERE competitor_id = ? 
                  AND first_seen >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                ORDER BY first_seen DESC
            """, [competitor['id']])
            
            total_pages = db.query_one("""
                SELECT COUNT(*) AS total FROM competitor_pages 
                WHERE competitor_id = ? AND removed_at IS NULL
            """, [competitor['id']])
            
            competitors_data.append({
                'id': competitor['id'],
                'name': competitor['name'],
                'domain': competitor['domain'],
                'total_pages': int(total_pages['total']),
                'new_this_week': [
                    {'title': p['title'], 'h1': p['h1'], 'word_count': p['word_count']}
                    for p in new_this_week
                ],
                'recent_topics': [p['title'] for p in recent_pages if p['title']]
            })
        
        except Exception as e:
            print(f"    ✗ Error con {competitor['name']}: {e}")
            continue
    
    # Datos del cliente para comparar
    client_info = {
        'name': client['name'],
        'domain': client['domain']
    }
    
    own_pages_count = db.query_one("""
        SELECT COUNT(DISTINCT url) AS total
        FROM page_audits
        WHERE client_id = ? 
          AND date = (SELECT MAX(date) FROM page_audits WHERE client_id = ?)
    """, [client_id, client_id])
    
    client_info['total_pages'] = int(own_pages_count['total']) if own_pages_count else 0
    
    # Generar insights con Gemini
    insights = generate_insights(client_id, client_info, competitors_data)
    
    if insights:
        save_insights(client_id, insights)
        print(f"  ✓ {len(insights)} insights generados y guardados")
    
    # Resumen final
    total_new = sum(s['new'] for s in all_stats)
    total_removed = sum(s['removed'] for s in all_stats)
    
    save_run(
        client_id, 'success',
        input_summary={'competitors': len(competitors)},
        output_summary={
            'new_pages': total_new,
            'removed_pages': total_removed,
            'insights_generated': len(insights)
        }
    )
    
    print(f"  ✓ Total nuevas: {total_new} · Eliminadas: {total_removed} · Insights: {len(insights)}")


def main():
    if len(sys.argv) > 1:
        client_id = int(sys.argv[1])
        print(f"Modo bajo demanda: cliente ID {client_id}")
        run_for_client(client_id)
    else:
        clients = db.query("""
            SELECT DISTINCT c.id 
            FROM clients c
            INNER JOIN competitors comp ON comp.client_id = c.id
            WHERE c.active = 1 AND comp.active = 1
        """)
        print(f"Modo masivo: {len(clients)} clientes con competidores definidos")
        for client in clients:
            try:
                run_for_client(client['id'])
            except Exception as e:
                print(f"Error con cliente {client['id']}: {e}")
                continue


if __name__ == '__main__':
    main()