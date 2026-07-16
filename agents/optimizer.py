"""
Agente Optimizador — FASE 1 (generación, sin escribir en las webs).

Rellena `task_affected_urls.suggested_value` para TODAS las tareas técnicas APROBADAS,
usando IA + búsquedas reales de Search Console:
  - add_meta_description -> meta description (IA)
  - fix_title_length     -> título SEO (IA)
  - fix_h1_count         -> H1 propuesto (IA, según tema + top query GSC)
  - add_schema           -> tipo de schema + JSON-LD recomendado (IA)  [suggested_value = JSON]
  - add_alt_to_images    -> alt descriptivo por imagen sin alt (IA)     [suggested_value = JSON]
  - add_canonical        -> canonical determinista (la propia URL)

NO escribe en WordPress. El valor queda como `suggested_value` para revisar en el
dashboard y aplicar después (fase 2, endpoint PHP).

Respeta el free-tier de Gemini: tope de URLs por ejecución + varias páginas por llamada.

Uso:
  python agents/optimizer.py [client_id] [--dry-run] [--limit N]
"""
import os
import sys
import json
import re
import time
import requests
from datetime import date, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry
from gsc_utils import get_gsc_service, fetch_page_queries

load_dotenv()
db = DBClient()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-3.5-flash'

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SEO-Agents-Optimizer/1.0'
TIMEOUT = 15
MAX_URLS_PER_RUN = int(os.getenv('OPTIMIZER_MAX_URLS', '25'))  # tope por ejecución (presupuesto Gemini)
GEMINI_BATCH = int(os.getenv('OPTIMIZER_BATCH', '5'))          # páginas por llamada a Gemini
MAX_IMAGES_PER_PAGE = 8                                        # alt: máximo de imágenes por página

DETERMINISTIC_TYPES = {'add_canonical'}
# Cada tipo IA -> campo del resultado de Gemini
FIELD_BY_TYPE = {
    'add_meta_description': 'meta',
    'fix_title_length': 'title',
    'fix_h1_count': 'h1',
    'add_schema': 'schema',
    'add_alt_to_images': 'alt',
}
AI_TYPES = set(FIELD_BY_TYPE.keys())

# Servicio GSC cacheado (se crea una vez por ejecución)
_GSC = {'service': None, 'tried': False}


def gsc_service():
    if not _GSC['tried']:
        _GSC['tried'] = True
        try:
            _GSC['service'] = get_gsc_service()
        except Exception as e:
            print(f"  ⚠ Search Console no disponible: {e}")
            _GSC['service'] = None
    return _GSC['service']


def gsc_date_range():
    """GSC tiene ~2-3 días de lag; usamos los últimos 90 días hasta hace 3 días."""
    end_d = date.today() - timedelta(days=3)
    start_d = end_d - timedelta(days=90)
    return start_d, end_d


def fetch_page_context(url, want_images=False):
    """Descarga la página y extrae title, h1, meta, extracto y (opcional) imágenes sin alt."""
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400 or 'text/html' not in r.headers.get('Content-Type', ''):
            return None
        soup = BeautifulSoup(r.content, 'lxml')
        title = (soup.find('title').text.strip() if soup.find('title') else '')[:300]
        h1_tag = soup.find('h1')
        h1 = (h1_tag.text.strip() if h1_tag else '')[:300]
        h1_count = len(soup.find_all('h1'))
        meta = soup.find('meta', attrs={'name': 'description'})
        meta_desc = (meta.get('content', '').strip() if meta else '')[:400]

        images = []
        if want_images:
            for img in soup.find_all('img'):
                alt = (img.get('alt') or '').strip()
                if alt:
                    continue
                src = img.get('src') or img.get('data-src') or ''
                if not src or src.startswith('data:'):
                    continue
                # pista de contexto: title, figcaption cercano o nombre de archivo
                hint = (img.get('title') or '').strip()
                if not hint:
                    fig = img.find_parent('figure')
                    cap = fig.find('figcaption') if fig else None
                    if cap and cap.text.strip():
                        hint = cap.text.strip()
                if not hint:
                    hint = os.path.basename(urlparse(src).path).rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')
                images.append({'src': src, 'hint': hint[:120]})
                if len(images) >= MAX_IMAGES_PER_PAGE:
                    break

        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        return {
            'title': title,
            'h1': h1,
            'h1_count': h1_count,
            'meta_description': meta_desc,
            'text_excerpt': text[:1200],
            'images_without_alt': images,
        }
    except Exception:
        return None


def normalize_canonical(url):
    """Canonical determinista: la propia URL sin query ni fragmento, con https."""
    p = urlparse(url)
    return f"https://{p.netloc}{p.path or '/'}"


def generate_ai_values(client, batch):
    """
    batch = lista de dicts: {url, needs(set), ctx, related_keyword, real_queries}
    Devuelve dict url -> {meta_description?, seo_title?, h1?, schema?, image_alts?}
    """
    pages = []
    for item in batch:
        needs = item['needs']
        ctx = item['ctx']
        page = {
            'url': item['url'],
            'needs': sorted(needs),
            'current_title': ctx.get('title', ''),
            'current_h1': ctx.get('h1', ''),
            'current_h1_count': ctx.get('h1_count', 0),
            'current_meta': ctx.get('meta_description', ''),
            'keyword': item.get('related_keyword') or '',
            'search_console_queries': item.get('real_queries', []),
            'content_excerpt': ctx.get('text_excerpt', '')[:900],
        }
        if 'alt' in needs:
            page['images_without_alt'] = ctx.get('images_without_alt', [])
        pages.append(page)

    system_prompt = f"""Eres un consultor SEO técnico senior para {client['name']} (web B2B en español).
Tono de la marca: {client.get('tone_of_voice') or 'profesional, claro, orientado a negocio'}.

Cada página trae "search_console_queries": BÚSQUEDAS REALES de Google Search Console por las que
ya recibe impresiones (clics, impresiones, posición). PRIORÍZALAS, sobre todo las de más
impresiones y posición 5-20. Si no hay queries, usa el contenido. No inventes servicios.

Para cada página genera SOLO los campos listados en su "needs":
- "meta"  -> meta_description: 120-155 caracteres, integra las búsquedas reales, con CTA suave.
- "title" -> seo_title: 35-60 caracteres, empieza por la búsqueda real más relevante si encaja.
             NO incluyas el nombre de la marca (Yoast lo añade con la plantilla).
- "h1"    -> h1: un ÚNICO H1 claro (20-70 car) con la búsqueda principal. (current_h1_count indica
             cuántos H1 hay ahora; si hay varios, propón el definitivo).
- "schema"-> schema: {{ "type": "<Article|Product|Service|LocalBusiness|FAQPage|BreadcrumbList|WebPage>",
             "jsonld": {{ ...schema.org JSON-LD mínimo y válido para esta página... }} }}
- "alt"   -> image_alts: array [{{ "src": "<src EXACTO recibido>", "alt": "texto alt 4-12 palabras, descriptivo, con término relevante si aplica" }}] para cada imagen de images_without_alt.

Responde SOLO con JSON válido:
{{ "results": [ {{ "url": "https://...", "meta_description": "...", "seo_title": "...", "h1": "...",
   "schema": {{...}}, "image_alts": [...] }} ] }}
Reglas:
- En cada objeto incluye SOLO los campos pedidos en "needs" (omite el resto).
- Respeta longitudes EXACTAS. Español natural, sin relleno genérico.
"""
    user_prompt = "Páginas:\n" + json.dumps(pages, ensure_ascii=False, indent=2)

    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_prompt)
    data = None
    for attempt in range(2):
        raw = generate_with_retry(
            model, user_prompt,
            generation_config={'temperature': 0.4, 'max_output_tokens': 8000, 'response_mime_type': 'application/json'}
        )
        text = raw.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        try:
            data = json.loads(text)
            break
        except Exception as e:
            if attempt == 0:
                print(f"    ⚠ JSON inválido, reintentando generación: {e}")
            else:
                print(f"    ⚠ JSON inválido de Gemini tras 2 intentos: {e}")
    if data is None:
        return {}
    out = {}
    for r in data.get('results', []):
        u = r.get('url')
        if u:
            out[u] = r
    return out


def value_for_type(res, task_type):
    """Extrae el suggested_value (texto) para un tipo de tarea del resultado de Gemini."""
    field = FIELD_BY_TYPE.get(task_type)
    if field == 'meta':
        return res.get('meta_description')
    if field == 'title':
        return res.get('seo_title')
    if field == 'h1':
        return res.get('h1')
    if field == 'schema':
        s = res.get('schema')
        return json.dumps(s, ensure_ascii=False) if s else None
    if field == 'alt':
        a = res.get('image_alts')
        return json.dumps(a, ensure_ascii=False) if a else None
    return None


def save_suggestion(url_id, suggested_value=None, notes=None):
    db.execute("""
        UPDATE task_affected_urls
        SET suggested_value = COALESCE(?, suggested_value),
            notes = COALESCE(?, notes)
        WHERE id = ?
    """, [suggested_value, notes, url_id])


def run_for_client(client_id, dry_run=False, limit=MAX_URLS_PER_RUN, only_types=None):
    client = db.query_one("SELECT id, name, domain, tone_of_voice, gsc_property FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return {'generated': 0, 'canonical': 0}

    print(f"\n{'='*60}\n  Optimizador · {client['name']} ({client['domain']}){'  [DRY-RUN]' if dry_run else ''}\n{'='*60}")

    service = gsc_service()
    prop = client.get('gsc_property')
    start_d, end_d = gsc_date_range()
    print(f"  Search Console: {'activo (' + prop + ')' if (service and prop) else 'no disponible → solo contenido'}")

    type_sql = ""
    type_params = []
    if only_types:
        placeholders = ','.join(['?'] * len(only_types))
        type_sql = f" AND t.task_type IN ({placeholders})"
        type_params = list(only_types)
        print(f"  Filtro de tipos: {', '.join(only_types)}")

    base_where = """t.client_id = ?
          AND t.status = 'approved'
          AND tau.url_status = 'pending'
          AND (tau.suggested_value IS NULL OR tau.suggested_value = '')""" + type_sql

    # Paso 1: URLs ÚNICAS pendientes (el límite cuenta páginas, no filas duplicadas)
    url_rows = db.query(f"""
        SELECT tau.page_url
        FROM task_affected_urls tau
        INNER JOIN tasks t ON t.id = tau.task_id
        WHERE {base_where}
        GROUP BY tau.page_url
        ORDER BY MIN(FIELD(t.priority, 'critical', 'high', 'medium', 'low')), MIN(tau.id)
        LIMIT ?
    """, [client_id] + type_params + [limit])

    if not url_rows:
        print("  ✓ Nada pendiente por generar.")
        return {'generated': 0, 'canonical': 0}

    wanted_urls = [r['page_url'] for r in url_rows]

    # Paso 2: TODAS las filas pendientes de esas URLs (para actualizar todos los duplicados)
    ph = ','.join(['?'] * len(wanted_urls))
    work = db.query(f"""
        SELECT tau.id, tau.page_url, tau.current_value, t.task_type, t.related_keyword
        FROM task_affected_urls tau
        INNER JOIN tasks t ON t.id = tau.task_id
        WHERE {base_where}
          AND tau.page_url IN ({ph})
    """, [client_id] + type_params + wanted_urls)

    print(f"  {len(wanted_urls)} páginas únicas · {len(work)} filas a actualizar")
    stats = {'generated': 0, 'canonical': 0}

    # 1) Canonical determinista (sin Gemini) + agrupar el resto por URL para IA
    ai_pending = {}  # url -> {ids:[(id,type)], needs:set, related_keyword}
    for row in work:
        tt = row['task_type']
        if tt in DETERMINISTIC_TYPES:
            val = normalize_canonical(row['page_url'])
            if not dry_run:
                save_suggestion(row['id'], suggested_value=val)
            stats['canonical'] += 1
        elif tt in AI_TYPES:
            e = ai_pending.setdefault(row['page_url'], {'ids': [], 'needs': set(), 'related_keyword': row['related_keyword']})
            e['ids'].append((row['id'], tt))
            e['needs'].add(FIELD_BY_TYPE[tt])
    if stats['canonical']:
        print(f"  [canonical] {stats['canonical']} generados")

    # 2) IA: fetch de contexto (con imágenes si hace falta alt) + generación batcheada
    urls = list(ai_pending.keys())
    print(f"  {len(urls)} URLs para IA (meta/title/h1/schema/alt)")
    batch = []
    for i, url in enumerate(urls):
        needs = ai_pending[url]['needs']
        ctx = fetch_page_context(url, want_images=('alt' in needs))
        time.sleep(0.4)
        if not ctx:
            print(f"    ⚠ No se pudo leer {url[:60]}")
            continue
        real_queries = []
        if service and prop:
            real_queries = fetch_page_queries(service, prop, url, start_d, end_d, limit=12)
            time.sleep(0.2)
        tag = ('GSC:' + real_queries[0]['query']) if real_queries else 'sin GSC'
        print(f"    · {url[:42]} [{','.join(sorted(needs))}] ({tag})")
        batch.append({'url': url, 'needs': needs, 'ctx': ctx,
                      'related_keyword': ai_pending[url]['related_keyword'], 'real_queries': real_queries})
        if len(batch) >= GEMINI_BATCH or i == len(urls) - 1:
            if batch:
                print(f"    → Gemini: generando para {len(batch)} páginas...")
                results = generate_ai_values(client, batch) if not dry_run else {}
                _apply_ai_results(ai_pending, batch, results, dry_run, stats)
                batch = []

    print(f"\n  Resumen {client['name']}: IA {stats['generated']} · canonical {stats['canonical']}")
    return stats


def _apply_ai_results(ai_pending, batch, results, dry_run, stats):
    for b in batch:
        url = b['url']
        res = results.get(url, {})
        for (url_id, tt) in ai_pending[url]['ids']:
            val = value_for_type(res, tt) if not dry_run else '[dry-run]'
            if not val:
                continue
            preview = val[:70].replace('\n', ' ') + ('…' if len(val) > 70 else '')
            print(f"      [{tt}] {url[:40]} -> {preview}")
            if not dry_run:
                save_suggestion(url_id, suggested_value=val)
            stats['generated'] += 1


def main():
    args = [a for a in sys.argv[1:]]
    dry_run = '--dry-run' in args
    limit = MAX_URLS_PER_RUN
    only_types = None
    skip_idx = set()
    if '--limit' in args:
        idx = args.index('--limit')
        try:
            limit = int(args[idx + 1])
            skip_idx.add(idx + 1)  # el valor del limit no es un client_id
        except Exception:
            pass
    if '--type' in args:
        idx = args.index('--type')
        try:
            only_types = [t.strip() for t in args[idx + 1].split(',') if t.strip()]
            skip_idx.add(idx + 1)
        except Exception:
            pass
    client_ids = [a for i, a in enumerate(args) if a.isdigit() and i not in skip_idx]

    if not GEMINI_API_KEY:
        print("ERROR: falta GEMINI_API_KEY")
        sys.exit(1)

    if client_ids:
        for cid in client_ids:
            run_for_client(int(cid), dry_run=dry_run, limit=limit, only_types=only_types)
    else:
        clients = db.query("SELECT id FROM clients WHERE active = 1 ORDER BY id")
        for c in clients:
            try:
                run_for_client(c['id'], dry_run=dry_run, limit=limit, only_types=only_types)
            except Exception as e:
                print(f"  ✗ Error cliente {c['id']}: {e}")


if __name__ == '__main__':
    main()