"""
Agente Espía v2 OPTIMIZADO - 3 llamadas a Gemini en lugar de 8.
Fusiona:
  - Llamada 1: pares de páginas + topics ganadores
  - Llamada 2: keyword gaps
  - Llamada 3: voz de marca
  - Estructura del sitio: sin Gemini (solo agrupación SQL)
"""
import os
import sys
import json
import time
import hashlib
import requests
from datetime import date
from urllib.parse import urlparse
from collections import defaultdict
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry, PACING_SPY

load_dotenv()
db = DBClient()


USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SEO-Agents-Spy/2.0'
TIMEOUT = 15
DEFAULT_MAX_URLS = 200
REQUEST_DELAY = 0.5

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = os.getenv('GEMINI_MODEL', 'gemini-flash-lite-latest')


def normalize_domain(domain):
    d = str(domain).strip()
    if '[' in d and ']' in d:
        d = d.split('[')[1].split(']')[0]
    d = d.replace('https://', '').replace('http://', '')
    d = d.split('/')[0].split('?')[0].split('#')[0]
    if d.startswith('www.'):
        d = d[4:]
    return d.strip()


def fetch(url, **kwargs):
    headers = {'User-Agent': USER_AGENT, **kwargs.pop('headers', {})}
    return requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, **kwargs)


def discover_urls(domain):
    domain = normalize_domain(domain)
    candidates = [
        f"https://{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/wp-sitemap.xml",
        f"https://www.{domain}/sitemap_index.xml",
        f"https://www.{domain}/sitemap.xml",
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


def analyze_page(url):
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
        
        headings = {'h2': [], 'h3': []}
        for h2 in soup.find_all('h2')[:20]:
            if h2.text:
                headings['h2'].append(h2.text.strip()[:200])
        for h3 in soup.find_all('h3')[:30]:
            if h3.text:
                headings['h3'].append(h3.text.strip()[:200])
        
        schema_scripts = soup.find_all('script', attrs={'type': 'application/ld+json'})
        has_schema = len(schema_scripts) > 0
        schema_types = []
        for script in schema_scripts:
            try:
                data = json.loads(script.string or '{}')
                if isinstance(data, dict):
                    t = data.get('@type', '')
                    if t:
                        schema_types.append(t if isinstance(t, str) else ','.join(t))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            t = item.get('@type', '')
                            if t:
                                schema_types.append(t if isinstance(t, str) else ','.join(t))
            except (json.JSONDecodeError, AttributeError):
                continue
        
        images = soup.find_all('img')
        images_with_alt = sum(1 for img in images if img.get('alt', '').strip())
        
        domain = urlparse(url).netloc
        internal_links = 0
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith('/') or domain in href:
                internal_links += 1
        
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        word_count = len(text.split())
        
        path = urlparse(url).path.strip('/')
        page_category = path.split('/')[0] if path else 'home'
        
        return {
            'title': title,
            'meta_description': meta_desc,
            'h1': h1,
            'word_count': word_count,
            'headings_json': json.dumps(headings, ensure_ascii=False),
            'has_schema': 1 if has_schema else 0,
            'schema_types': ','.join(set(schema_types))[:500],
            'images_count': len(images),
            'images_with_alt': images_with_alt,
            'internal_links_count': internal_links,
            'page_category': page_category[:100],
            'text_excerpt': text[:3000]
        }
    except Exception:
        return None


def get_existing_urls(competitor_id):
    rows = db.query("""
        SELECT url, url_hash FROM competitor_pages 
        WHERE competitor_id = ? AND removed_at IS NULL
    """, [competitor_id])
    return {r['url_hash']: r['url'] for r in rows}


def save_page(competitor_id, url, today, page_data, is_new):
    h = url_hash(url)
    if is_new:
        db.execute("""
            INSERT INTO competitor_pages 
                (competitor_id, url, url_hash, title, meta_description, h1, word_count, 
                 headings_json, has_schema, schema_types, images_count, images_with_alt, 
                 internal_links_count, page_category, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                last_seen = VALUES(last_seen),
                title = VALUES(title),
                meta_description = VALUES(meta_description),
                h1 = VALUES(h1),
                word_count = VALUES(word_count),
                headings_json = VALUES(headings_json),
                has_schema = VALUES(has_schema),
                schema_types = VALUES(schema_types),
                images_count = VALUES(images_count),
                images_with_alt = VALUES(images_with_alt),
                internal_links_count = VALUES(internal_links_count),
                page_category = VALUES(page_category),
                removed_at = NULL
        """, [
            competitor_id, url[:1000], h,
            page_data.get('title', ''), page_data.get('meta_description', ''),
            page_data.get('h1', ''), page_data.get('word_count', 0),
            page_data.get('headings_json', '{}'), page_data.get('has_schema', 0),
            page_data.get('schema_types', ''), page_data.get('images_count', 0),
            page_data.get('images_with_alt', 0), page_data.get('internal_links_count', 0),
            page_data.get('page_category', ''), today, today
        ])
    else:
        db.execute("""
            UPDATE competitor_pages SET last_seen = ?
            WHERE competitor_id = ? AND url_hash = ?
        """, [today, competitor_id, h])


def mark_removed(competitor_id, url_hashes_disappeared, today):
    for h in url_hashes_disappeared:
        db.execute("""
            UPDATE competitor_pages SET removed_at = ?
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


def spy_on_competitor(competitor, max_urls=DEFAULT_MAX_URLS):
    print(f"\n  Espiando: {competitor['name']} ({competitor['domain']})")
    urls_found = discover_urls(competitor['domain'])
    if not urls_found:
        print(f"    ⚠ No se pudo acceder al sitemap")
        return {'new': 0, 'existing': 0, 'removed': 0, 'analyzed': 0}
    
    total_found = len(set(urls_found))
    urls_found = list(set(urls_found))[:max_urls]
    
    if total_found > max_urls:
        print(f"    Encontradas {total_found} URLs en sitemap (limitando a {max_urls})")
    else:
        print(f"    Encontradas {len(urls_found)} URLs en sitemap")
    
    existing = get_existing_urls(competitor['id'])
    found_hashes = {url_hash(u) for u in urls_found}
    new_urls = [u for u in urls_found if url_hash(u) not in existing]
    disappeared = set(existing.keys()) - found_hashes
    today = date.today().isoformat()
    
    analyzed = 0
    for url in new_urls:
        page_data = analyze_page(url)
        if page_data:
            save_page(competitor['id'], url, today, page_data, is_new=True)
            analyzed += 1
        else:
            save_page(competitor['id'], url, today, {}, is_new=True)
        time.sleep(REQUEST_DELAY)
    
    for url in urls_found:
        if url_hash(url) in existing:
            save_page(competitor['id'], url, today, {}, is_new=False)
    
    mark_removed(competitor['id'], list(disappeared), today)
    print(f"    ✓ Nuevas: {len(new_urls)} · Eliminadas: {len(disappeared)}")
    
    return {
        'new': len(new_urls),
        'existing': len(existing) - len(disappeared),
        'removed': len(disappeared),
        'analyzed': analyzed
    }


def analyze_client_pages(client_id, client_domain, max_urls=DEFAULT_MAX_URLS):
    print(f"\n  → Analizando páginas del cliente...")
    urls = discover_urls(client_domain)
    if not urls:
        print(f"    ⚠ No se pudo acceder al sitemap del cliente")
        return []
    
    total_found = len(set(urls))
    urls = list(set(urls))[:max_urls]
    
    if total_found > max_urls:
        print(f"    {total_found} URLs encontradas (limitando a {max_urls})")
    else:
        print(f"    {len(urls)} URLs del cliente")
    
    pages = []
    for url in urls:
        page_data = analyze_page(url)
        if page_data:
            pages.append({'url': url, **page_data})
        time.sleep(REQUEST_DELAY)
    
    print(f"    ✓ {len(pages)} páginas analizadas")
    return pages


def call_gemini(system_prompt, user_prompt, max_tokens=8000, expect_json=True, label="Análisis"):
    """Llamada a Gemini con streaming y manejo de errores."""
    if not GEMINI_API_KEY:
        return None
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_prompt)
        config = {
            'temperature': 0.3,
            'max_output_tokens': max_tokens,
        }
        if expect_json:
            config['response_mime_type'] = 'application/json'

        print(f'    {label} ', end='', flush=True)
        progress = {'last_dot': 0}

        def on_progress(chars):
            while progress['last_dot'] + 200 <= chars:
                print('.', end='', flush=True)
                progress['last_dot'] += 200

        raw_text = generate_with_retry(model, user_prompt, config, on_progress=on_progress)
        print(f" [{len(raw_text)} car.]")

        if expect_json:
            return json.loads(raw_text.strip())
        return raw_text.strip()
    except Exception as e:
        print(f"\n    ⚠ Error en Gemini: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# LLAMADA 1 FUSIONADA: Pares de páginas + Topics ganadores
# ═══════════════════════════════════════════════════════════════════
def analyze_pairs_and_topics(client_id, client_pages, competitors_data):
    """Llamada combinada: encuentra pares de páginas equivalentes Y detecta topics ganadores en una sola pasada."""
    print(f"\n  [1/3] Análisis combinado: pares + topics ganadores...")
    db.execute("DELETE FROM competitor_page_pairs WHERE client_id = ?", [client_id])
    db.execute("DELETE FROM competitor_winning_topics WHERE client_id = ?", [client_id])
    
    if not client_pages:
        print(f"    ⚠ Sin páginas del cliente")
        return {'pairs': 0, 'topics': 0}
    
    # Páginas del cliente (resumidas)
    client_summary = [
        {
            'url': p['url'],
            'title': p['title'],
            'h1': p['h1'],
            'word_count': p['word_count'],
            'category': p['page_category']
        }
        for p in client_pages[:25]
    ]
    
    # Páginas de cada competidor (resumidas)
    competitors_summary = {}
    competitors_pages_lookup = {}
    
    for comp in competitors_data:
        comp_pages = db.query("""
            SELECT url, title, h1, word_count, page_category
            FROM competitor_pages
            WHERE competitor_id = ? AND removed_at IS NULL AND title != ''
            ORDER BY word_count DESC LIMIT 25
        """, [comp['id']])
        
        competitors_summary[comp['name']] = [
            {
                'url': p['url'],
                'title': p['title'],
                'h1': p['h1'],
                'word_count': int(p['word_count'] or 0),
                'category': p['page_category']
            }
            for p in comp_pages
        ]
        competitors_pages_lookup[comp['name']] = {
            'id': comp['id'],
            'pages': comp_pages
        }
    
    system_prompt = """Eres un analista SEO senior B2B español. Tu trabajo es analizar un cliente y sus competidores y devolver DOS análisis a la vez:

1. PARES de páginas equivalentes (hasta 5 por competidor)
2. TOPICS ganadores donde los competidores tienen más cobertura

Responde SOLO con JSON válido en este formato exacto:

{
  "pairs": [
    {
      "competitor_name": "Bigblue",
      "client_url": "https://...",
      "competitor_url": "https://...",
      "similarity_score": 85.5,
      "match_reasoning": "Ambas tratan de almacenaje B2B",
      "gap_analysis": "El competidor tiene 1247 palabras vs 340 del cliente. Cubre trazabilidad y CSRD que el cliente no menciona.",
      "recommendations": "Ampliar contenido a 1000+ palabras, añadir sección sobre cumplimiento, incluir casos de uso"
    }
  ],
  "topics": [
    {
      "topic": "Sostenibilidad logística y huella de carbono",
      "competitor_coverage_avg": 4,
      "client_coverage": 0,
      "opportunity_score": 90,
      "reasoning": "3 competidores tienen entre 3 y 6 páginas. Cliente no tiene ninguna. Tendencia ESG B2B.",
      "example_urls": ["https://amphora.com/sostenibilidad"]
    }
  ]
}

REGLAS PARES:
- Empareja páginas con similarity_score > 60
- Máximo 5 pares por competidor (3 competidores = hasta 15 pares total)
- gap_analysis SIEMPRE con números reales del contexto
- recommendations: acciones específicas, no genéricas

REGLAS TOPICS:
- 5-8 topics donde los competidores te superan
- opportunity_score: 0-100 (90+ = oportunidad muy clara)
- Solo topics con disparidad real
- Sé específico ("marketing automation para logística", no "marketing")
"""
    
    user_prompt = f"""Cliente: {len(client_summary)} páginas

{json.dumps(client_summary, indent=2, ensure_ascii=False)}

Competidores: {len(competitors_summary)}

{json.dumps(competitors_summary, indent=2, ensure_ascii=False)}

Genera los dos análisis (pairs + topics) en un único JSON."""
    
    result = call_gemini(system_prompt, user_prompt, max_tokens=10000, label="Pares + Topics")
    
    if not result:
        return {'pairs': 0, 'topics': 0}
    
    # Guardar pares
    pairs_saved = 0
    for pair in result.get('pairs', []):
        comp_name = pair.get('competitor_name', '')
        if comp_name not in competitors_pages_lookup:
            continue
        comp_data = competitors_pages_lookup[comp_name]
        
        client_p = next((p for p in client_pages if p['url'] == pair.get('client_url')), None)
        comp_p = next((p for p in comp_data['pages'] if p['url'] == pair.get('competitor_url')), None)
        
        if not client_p or not comp_p:
            continue
        
        try:
            db.execute("""
                INSERT INTO competitor_page_pairs 
                    (client_id, competitor_id, client_url, competitor_url,
                     client_title, client_word_count, client_h1,
                     competitor_title, competitor_word_count, competitor_h1,
                     similarity_score, match_reasoning, gap_analysis, recommendations)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                client_id, comp_data['id'],
                client_p['url'][:1000], comp_p['url'][:1000],
                client_p['title'][:500], client_p['word_count'], client_p['h1'][:500],
                comp_p['title'][:500] if comp_p['title'] else '',
                int(comp_p['word_count'] or 0),
                comp_p['h1'][:500] if comp_p['h1'] else '',
                pair.get('similarity_score', 0),
                pair.get('match_reasoning', ''),
                pair.get('gap_analysis', ''),
                pair.get('recommendations', '')
            ])
            pairs_saved += 1
        except Exception as e:
            print(f"    ⚠ Error guardando par: {e}")
    
    # Guardar topics
    topics_saved = 0
    for topic in result.get('topics', []):
        try:
            db.execute("""
                INSERT INTO competitor_winning_topics 
                    (client_id, topic, competitor_coverage_avg, client_coverage,
                     opportunity_score, reasoning, example_competitor_urls)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                client_id, topic.get('topic', '')[:300],
                topic.get('competitor_coverage_avg', 0),
                topic.get('client_coverage', 0),
                topic.get('opportunity_score', 0),
                topic.get('reasoning', ''),
                json.dumps(topic.get('example_urls', []), ensure_ascii=False)
            ])
            topics_saved += 1
        except Exception as e:
            print(f"    ⚠ Error guardando topic: {e}")
    
    print(f"    ✓ {pairs_saved} pares + {topics_saved} topics guardados")
    return {'pairs': pairs_saved, 'topics': topics_saved}


# ═══════════════════════════════════════════════════════════════════
# LLAMADA 2: Keywords donde te superan
# ═══════════════════════════════════════════════════════════════════
def analyze_keyword_gaps(client_id, client_pages, competitors_data):
    print(f"\n  [2/3] Analizando keywords donde te superan...")
    db.execute("DELETE FROM competitor_keyword_gaps WHERE client_id = ?", [client_id])
    
    opportunities = db.query("""
        SELECT 
            keyword,
            AVG(position) AS position,
            SUM(impressions) AS impressions
        FROM metrics_keywords_daily
        WHERE client_id = ?
          AND date >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
        GROUP BY keyword
        HAVING position BETWEEN 5 AND 15 AND impressions >= 30
        ORDER BY impressions DESC
        LIMIT 10
    """, [client_id])
    
    if not opportunities:
        print(f"    ⚠ Sin oportunidades de keywords (pos 5-15)")
        return 0
    
    print(f"    {len(opportunities)} keywords a analizar")
    
    all_comp_pages = []
    for comp in competitors_data:
        comp_pages = db.query("""
            SELECT url, title, h1, word_count, has_schema, page_category
            FROM competitor_pages
            WHERE competitor_id = ? AND removed_at IS NULL AND word_count > 100
            ORDER BY word_count DESC LIMIT 40
        """, [comp['id']])
        for p in comp_pages:
            all_comp_pages.append({
                'competitor_id': comp['id'],
                'competitor_name': comp['name'],
                'url': p['url'],
                'title': p['title'],
                'h1': p['h1'],
                'word_count': int(p['word_count'] or 0),
                'has_schema': bool(p['has_schema']),
                'category': p['page_category']
            })
    
    if not all_comp_pages:
        print(f"    ⚠ Sin páginas de competidores con contenido")
        return 0
    
    system_prompt = """Eres un analista SEO competitivo. Para cada keyword del cliente, identifica qué páginas de competidores cubren mejor esa keyword.

Responde SOLO con JSON:

{
  "gaps": [
    {
      "keyword": "transporte refrigerado barcelona",
      "competitor_url": "https://amphora.../transporte-refrigerado",
      "competitor_name": "Amphora",
      "competitor_word_count": 1247,
      "competitor_has_schema": true,
      "competitor_h1": "Servicios de transporte refrigerado en Barcelona",
      "recommendation": "Esta página tiene 2.5x más contenido. Acciones: ampliar página, añadir FAQ, optimizar H1."
    }
  ]
}

Reglas:
- Solo incluye gaps donde haya un competidor con mejor página
- Si ningún competidor parece optimizado para la keyword, omítela
- Recomendaciones ACCIONABLES y específicas
"""
    
    user_prompt = f"""Keywords del cliente con su posición:
{json.dumps([{'keyword': o['keyword'], 'position': float(o['position'])} for o in opportunities], indent=2, ensure_ascii=False)}

Páginas de competidores:
{json.dumps(all_comp_pages[:50], indent=2, ensure_ascii=False)}

Identifica qué competidor cubre mejor cada keyword."""
    
    result = call_gemini(system_prompt, user_prompt, max_tokens=6000, label="Keyword gaps")
    
    if not result or 'gaps' not in result:
        return 0
    
    gaps_saved = 0
    for gap in result.get('gaps', []):
        keyword = gap.get('keyword', '')
        comp_url = gap.get('competitor_url', '')
        comp_page = next((p for p in all_comp_pages if p['url'] == comp_url), None)
        if not comp_page:
            continue
        opp = next((o for o in opportunities if o['keyword'] == keyword), None)
        if not opp:
            continue
        
        try:
            db.execute("""
                INSERT INTO competitor_keyword_gaps 
                    (client_id, competitor_id, keyword,
                     client_position, competitor_url, competitor_word_count, 
                     competitor_has_schema, competitor_h1, recommendation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                client_id, comp_page['competitor_id'], keyword[:500],
                float(opp['position']), comp_url[:1000],
                comp_page['word_count'], 1 if comp_page['has_schema'] else 0,
                comp_page['h1'][:500] if comp_page['h1'] else '',
                gap.get('recommendation', '')
            ])
            gaps_saved += 1
        except Exception as e:
            print(f"    ⚠ Error guardando gap: {e}")
    
    print(f"    ✓ {gaps_saved} keyword gaps")
    return gaps_saved


# ═══════════════════════════════════════════════════════════════════
# Estructura del sitio (sin Gemini)
# ═══════════════════════════════════════════════════════════════════
def analyze_site_structure(client_id, competitors_data):
    print(f"\n  [SQL] Analizando estructura de sitios...")
    db.execute("DELETE FROM competitor_site_structure WHERE client_id = ?", [client_id])
    
    total_saved = 0
    for competitor in competitors_data:
        pages = db.query("""
            SELECT page_category, url FROM competitor_pages
            WHERE competitor_id = ? AND removed_at IS NULL
        """, [competitor['id']])
        
        if not pages:
            continue
        
        categories = defaultdict(list)
        for p in pages:
            cat = p['page_category'] or 'home'
            categories[cat].append(p['url'])
        
        for cat, urls in categories.items():
            db.execute("""
                INSERT INTO competitor_site_structure 
                    (client_id, competitor_id, category, pages_count, example_urls)
                VALUES (?, ?, ?, ?, ?)
            """, [
                client_id, competitor['id'], cat[:200],
                len(urls),
                json.dumps(urls[:5], ensure_ascii=False)
            ])
            total_saved += 1
        
        print(f"    {competitor['name']}: {len(categories)} categorías")
    
    print(f"    ✓ {total_saved} categorías guardadas")
    return total_saved


# ═══════════════════════════════════════════════════════════════════
# LLAMADA 3: Voz de marca
# ═══════════════════════════════════════════════════════════════════
def analyze_brand_voice(client_id, client_pages, competitors_data):
    print(f"\n  [3/3] Analizando voz de marca...")
    db.execute("DELETE FROM competitor_brand_voice WHERE client_id = ?", [client_id])
    
    # Muestras del cliente (las que ya tenemos en memoria)
    client_samples = []
    for p in client_pages[:5]:
        if p.get('text_excerpt'):
            client_samples.append({
                'title': p.get('title', ''),
                'text': p.get('text_excerpt', '')[:1200]
            })
    
    # Muestras de competidores: cogemos solo títulos + h1 + headings de la BD
    # (sin re-fetch, así ahorramos peticiones HTTP)
    comp_samples_by_id = {}
    for comp in competitors_data:
        pages = db.query("""
            SELECT title, h1, meta_description, headings_json
            FROM competitor_pages
            WHERE competitor_id = ? AND removed_at IS NULL 
              AND word_count > 200
            ORDER BY word_count DESC LIMIT 5
        """, [comp['id']])
        
        samples = []
        for p in pages:
            headings = {}
            try:
                headings = json.loads(p['headings_json'] or '{}')
            except:
                pass
            
            text_blob = f"Title: {p['title']}\nH1: {p['h1']}\nMeta: {p['meta_description']}\n"
            if headings.get('h2'):
                text_blob += "H2s: " + " | ".join(headings['h2'][:5])
            
            samples.append({
                'title': p['title'],
                'text': text_blob[:1200]
            })
        
        if samples:
            comp_samples_by_id[comp['id']] = {
                'name': comp['name'],
                'samples': samples
            }
    
    if not client_samples:
        print(f"    ⚠ Sin muestras del cliente")
        return 0
    
    system_prompt = """Eres experto en comunicación B2B y voz de marca. Analiza el tono y estilo de varias empresas.

Para cada empresa evalúa:
- tone_label: etiqueta corta ("Corporativo data-driven", "Cercano conversacional", "Técnico industrial")
- tone_description: 2-3 frases describiendo el tono
- formality_score: 0-100 (0=informal, 100=muy formal)
- technical_score: 0-100 (0=sin jerga, 100=muy técnico)
- emotional_score: 0-100 (0=racional, 100=muy emocional)
- example_phrases: 2-3 frases del texto que ejemplifican el tono

Para el CLIENTE, añade:
- positioning_suggestion: cómo diferenciarse vs competidores

Responde SOLO con JSON:

{
  "client_analysis": {
    "tone_label": "...",
    "tone_description": "...",
    "formality_score": 75,
    "technical_score": 60,
    "emotional_score": 30,
    "example_phrases": ["...", "..."],
    "positioning_suggestion": "Tu tono es similar a X. Diferénciate Y."
  },
  "competitor_analyses": [
    {
      "competitor_id": 123,
      "tone_label": "...",
      "tone_description": "...",
      "formality_score": 60,
      "technical_score": 70,
      "emotional_score": 40,
      "example_phrases": ["...", "..."]
    }
  ]
}
"""
    
    user_prompt = f"""CLIENTE - Muestras:
{json.dumps(client_samples, indent=2, ensure_ascii=False)}

COMPETIDORES - Muestras:
{json.dumps([{'competitor_id': cid, 'name': d['name'], 'samples': d['samples']} for cid, d in comp_samples_by_id.items()], indent=2, ensure_ascii=False)}

Analiza voz de marca y propón posicionamiento para el cliente."""
    
    result = call_gemini(system_prompt, user_prompt, max_tokens=5000, label="Voz de marca")
    
    if not result:
        return 0
    
    ca = result.get('client_analysis', {})
    saved = 0
    if ca:
        try:
            db.execute("""
                INSERT INTO competitor_brand_voice 
                    (client_id, competitor_id, is_client,
                     tone_label, tone_description, formality_score, technical_score, emotional_score,
                     example_phrases, positioning_suggestion)
                VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?, ?)
            """, [
                client_id,
                ca.get('tone_label', '')[:100],
                ca.get('tone_description', ''),
                ca.get('formality_score', 50),
                ca.get('technical_score', 50),
                ca.get('emotional_score', 50),
                json.dumps(ca.get('example_phrases', []), ensure_ascii=False),
                ca.get('positioning_suggestion', '')
            ])
            saved = 1
        except Exception as e:
            print(f"    ⚠ Error guardando voz cliente: {e}")
    
    for ca_comp in result.get('competitor_analyses', []):
        comp_id = ca_comp.get('competitor_id')
        if not comp_id:
            continue
        try:
            db.execute("""
                INSERT INTO competitor_brand_voice 
                    (client_id, competitor_id, is_client,
                     tone_label, tone_description, formality_score, technical_score, emotional_score,
                     example_phrases, positioning_suggestion)
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, NULL)
            """, [
                client_id, comp_id,
                ca_comp.get('tone_label', '')[:100],
                ca_comp.get('tone_description', ''),
                ca_comp.get('formality_score', 50),
                ca_comp.get('technical_score', 50),
                ca_comp.get('emotional_score', 50),
                json.dumps(ca_comp.get('example_phrases', []), ensure_ascii=False)
            ])
            saved += 1
        except Exception as e:
            print(f"    ⚠ Error guardando voz competidor: {e}")
    
    print(f"    ✓ {saved} análisis de voz")
    return saved


# ═══════════════════════════════════════════════════════════════════
# Ejecución principal
# ═══════════════════════════════════════════════════════════════════
def run_for_client(client_id):
    client = db.query_one("""
        SELECT id, name, domain, COALESCE(spy_max_urls, ?) AS spy_max_urls
        FROM clients WHERE id = ?
    """, [DEFAULT_MAX_URLS, client_id])
    
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return
    
    max_urls = int(client['spy_max_urls'])
    
    print(f"\n{'='*60}")
    print(f"  Espía v2 OPTIMIZADO · {client['name']} ({client['domain']})")
    print(f"  Límite URLs: {max_urls} · Llamadas Gemini estimadas: 3")
    print(f"{'='*60}")
    
    competitors = db.query("""
        SELECT id, domain, name FROM competitors 
        WHERE client_id = ? AND active = 1
    """, [client_id])
    
    if not competitors:
        print(f"  ⚠ Sin competidores definidos.")
        save_run(client_id, 'error', error='No competitors defined')
        return
    
    print(f"  Competidores activos: {len(competitors)}")
    
    all_stats = []
    for competitor in competitors:
        try:
            stats = spy_on_competitor(competitor, max_urls=max_urls)
            all_stats.append({**stats, 'competitor': competitor['name']})
        except Exception as e:
            print(f"    ✗ Error con {competitor['name']}: {e}")
            continue
    
    client_pages = analyze_client_pages(client_id, client['domain'], max_urls=max_urls)
    competitors_data = [{'id': c['id'], 'name': c['name'], 'domain': c['domain']} for c in competitors]
    
    results = {}
    
    # Llamada 1: pares + topics
    try:
        pt_result = analyze_pairs_and_topics(client_id, client_pages, competitors_data)
        results['pairs'] = pt_result['pairs']
        results['winning_topics'] = pt_result['topics']
    except Exception as e:
        print(f"    ✗ Error en pares+topics: {e}")
        results['pairs'] = 0
        results['winning_topics'] = 0
    
    # Estructura: sin Gemini
    try:
        results['site_structure'] = analyze_site_structure(client_id, competitors_data)
    except Exception as e:
        print(f"    ✗ Error en estructura: {e}")
        results['site_structure'] = 0
    
    # Llamada 2: keyword gaps
    try:
        results['keyword_gaps'] = analyze_keyword_gaps(client_id, client_pages, competitors_data)
    except Exception as e:
        print(f"    ✗ Error en keyword_gaps: {e}")
        results['keyword_gaps'] = 0
    
    # Llamada 3: voz de marca
    try:
        results['brand_voice'] = analyze_brand_voice(client_id, client_pages, competitors_data)
    except Exception as e:
        print(f"    ✗ Error en brand_voice: {e}")
        results['brand_voice'] = 0
    
    total_new = sum(s['new'] for s in all_stats)
    total_removed = sum(s['removed'] for s in all_stats)
    
    save_run(
        client_id, 'success',
        input_summary={
            'competitors': len(competitors), 
            'client_pages_analyzed': len(client_pages),
            'max_urls_limit': max_urls,
            'gemini_calls': 3
        },
        output_summary={
            'new_pages': total_new,
            'removed_pages': total_removed,
            **results
        }
    )
    
    print(f"\n{'='*60}")
    print(f"  Resumen: páginas nuevas {total_new} · eliminadas {total_removed}")
    print(f"  Pares: {results['pairs']} · Keyword gaps: {results['keyword_gaps']}")
    print(f"  Categorías: {results['site_structure']} · Topics ganadores: {results['winning_topics']}")
    print(f"  Voz de marca: {results['brand_voice']} análisis")
    print(f"  Llamadas Gemini realizadas: 3")
    print(f"{'='*60}\n")


def main():
    if len(sys.argv) > 1:
        client_id = int(sys.argv[1])
        run_for_client(client_id)
    else:
        clients = db.query("""
            SELECT DISTINCT c.id FROM clients c
            INNER JOIN competitors comp ON comp.client_id = c.id
            WHERE c.active = 1 AND comp.active = 1
        """)
        for i, client in enumerate(clients):
            try:
                run_for_client(client['id'])
            except Exception as e:
                print(f"Error con cliente {client['id']}: {e}")
                continue
            # Pausa entre clientes para no saturar el límite de 5 req/min de Gemini
            if i < len(clients) - 1:
                print(f"  ⏸  Pausa de {PACING_SPY}s antes del siguiente cliente...")
                time.sleep(PACING_SPY)


if __name__ == '__main__':
    main()