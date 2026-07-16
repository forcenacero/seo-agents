"""
Agente Optimizador — FASE 1 (generación, sin escribir en las webs).

Rellena `task_affected_urls.suggested_value` para las tareas técnicas APROBADAS:
  - add_meta_description  -> genera meta description con Gemini
  - fix_title_length      -> genera título SEO con Gemini
  - add_canonical         -> canonical determinista (la propia URL)
  - add_alt_to_images / add_schema / fix_h1_count -> nota para revisión/aplicación manual
    (no se pueden aplicar de forma fiable vía meta Yoast; se resuelven en fase de plantilla)

NO escribe en WordPress. El valor generado queda como `suggested_value` para que lo
revises en el dashboard y luego se aplique (fase 2, endpoint PHP).

Respeta el free-tier de Gemini: procesa como máximo MAX_URLS_PER_RUN por ejecución y
batchea varias páginas por llamada.

Uso:
  python agents/optimizer.py [client_id] [--dry-run] [--limit N]
"""
import os
import sys
import json
import re
import time
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry

load_dotenv()
db = DBClient()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-3.5-flash'

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SEO-Agents-Optimizer/1.0'
TIMEOUT = 15
MAX_URLS_PER_RUN = int(os.getenv('OPTIMIZER_MAX_URLS', '25'))  # tope por ejecución (presupuesto Gemini)
GEMINI_BATCH = int(os.getenv('OPTIMIZER_BATCH', '6'))          # páginas por llamada a Gemini

# Tipos que generamos por completo en esta fase
AI_TYPES = {'add_meta_description', 'fix_title_length'}
DETERMINISTIC_TYPES = {'add_canonical'}
# Tipos que quedan para fase de plantilla/manual
MANUAL_TYPES = {'add_alt_to_images', 'add_schema', 'fix_h1_count'}
MANUAL_NOTE = {
    'add_alt_to_images': 'Requiere fase 2: generar alt por imagen y aplicar en media/contenido.',
    'add_schema': 'Schema suele gestionarlo Yoast a nivel global; revisar plantilla/plugin.',
    'fix_h1_count': 'El H1 vive en el contenido/plantilla; requiere edición manual del tema.',
}


def fetch_page_context(url):
    """Descarga la página y extrae title, h1, meta y un extracto de texto."""
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400 or 'text/html' not in r.headers.get('Content-Type', ''):
            return None
        soup = BeautifulSoup(r.content, 'lxml')
        title = (soup.find('title').text.strip() if soup.find('title') else '')[:300]
        h1_tag = soup.find('h1')
        h1 = (h1_tag.text.strip() if h1_tag else '')[:300]
        meta = soup.find('meta', attrs={'name': 'description'})
        meta_desc = (meta.get('content', '').strip() if meta else '')[:400]
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        return {
            'title': title,
            'h1': h1,
            'meta_description': meta_desc,
            'text_excerpt': text[:1500]
        }
    except Exception:
        return None


def normalize_canonical(url):
    """Canonical determinista: la propia URL sin query ni fragmento, con https."""
    p = urlparse(url)
    scheme = 'https'
    path = p.path or '/'
    return f"{scheme}://{p.netloc}{path}"


def generate_ai_values(client, batch):
    """
    batch = lista de dicts:
      {url, need_meta(bool), need_title(bool), ctx{title,h1,meta_description,text_excerpt}, related_keyword}
    Devuelve dict url -> {meta_description, seo_title} (solo los campos pedidos).
    """
    pages = []
    for item in batch:
        pages.append({
            'url': item['url'],
            'need_meta': item['need_meta'],
            'need_title': item['need_title'],
            'current_title': item['ctx'].get('title', ''),
            'h1': item['ctx'].get('h1', ''),
            'current_meta': item['ctx'].get('meta_description', ''),
            'keyword': item.get('related_keyword') or '',
            'content_excerpt': item['ctx'].get('text_excerpt', '')[:1200],
        })

    system_prompt = f"""Eres un copywriter SEO senior para {client['name']} (web B2B en español).
Tono de la marca: {client.get('tone_of_voice') or 'profesional, claro, orientado a negocio'}.

Para cada página, genera SOLO lo que se pide (need_meta / need_title):
- meta_description: 120-155 caracteres, incluye la keyword si la hay, con gancho y CTA suave. Sin comillas.
- seo_title: 35-60 caracteres, incluye keyword al principio si aplica. NO añadas el nombre de la marca
  (Yoast lo agrega con la plantilla). Sin comillas.

Responde SOLO con JSON válido:
{{
  "results": [
    {{ "url": "https://...", "meta_description": "...", "seo_title": "..." }}
  ]
}}
Reglas:
- Incluye en cada objeto solo los campos solicitados para esa URL (omite el que no se pida).
- Respeta los límites de longitud EXACTAMENTE.
- Español natural, nada de relleno genérico.
"""
    user_prompt = "Páginas:\n" + json.dumps(pages, ensure_ascii=False, indent=2)

    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_prompt)
    data = None
    for attempt in range(2):
        raw = generate_with_retry(
            model, user_prompt,
            generation_config={'temperature': 0.4, 'max_output_tokens': 4000, 'response_mime_type': 'application/json'}
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
            out[u] = {k: r[k] for k in ('meta_description', 'seo_title') if k in r and r[k]}
    return out


def save_suggestion(url_id, suggested_value=None, notes=None):
    db.execute("""
        UPDATE task_affected_urls
        SET suggested_value = COALESCE(?, suggested_value),
            notes = COALESCE(?, notes)
        WHERE id = ?
    """, [suggested_value, notes, url_id])


def run_for_client(client_id, dry_run=False, limit=MAX_URLS_PER_RUN):
    client = db.query_one("SELECT id, name, domain, tone_of_voice FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return {'generated': 0, 'manual': 0, 'canonical': 0}

    print(f"\n{'='*60}\n  Optimizador · {client['name']} ({client['domain']}){'  [DRY-RUN]' if dry_run else ''}\n{'='*60}")

    work = db.query("""
        SELECT tau.id, tau.page_url, tau.current_value, t.task_type, t.related_keyword
        FROM task_affected_urls tau
        INNER JOIN tasks t ON t.id = tau.task_id
        WHERE t.client_id = ?
          AND t.status = 'approved'
          AND tau.url_status = 'pending'
          AND (tau.suggested_value IS NULL OR tau.suggested_value = '')
        ORDER BY FIELD(t.priority, 'critical', 'high', 'medium', 'low'), tau.id
        LIMIT ?
    """, [client_id, limit])

    if not work:
        print("  ✓ Nada pendiente por generar.")
        return {'generated': 0, 'manual': 0, 'canonical': 0}

    print(f"  {len(work)} URLs a procesar (tope {limit})")

    stats = {'generated': 0, 'manual': 0, 'canonical': 0}

    # 1) Canonical (determinista) y manual (nota) — sin Gemini
    ai_pending = {}  # url -> {row, need_meta, need_title}
    for row in work:
        tt = row['task_type']
        if tt in DETERMINISTIC_TYPES:
            val = normalize_canonical(row['page_url'])
            print(f"    [canonical] {row['page_url'][:60]} -> {val}")
            if not dry_run:
                save_suggestion(row['id'], suggested_value=val)
            stats['canonical'] += 1
        elif tt in MANUAL_TYPES:
            note = MANUAL_NOTE.get(tt, 'Requiere revisión manual.')
            if not dry_run:
                save_suggestion(row['id'], notes=note)
            stats['manual'] += 1
        elif tt in AI_TYPES:
            entry = ai_pending.setdefault(row['page_url'], {'ids': [], 'need_meta': False, 'need_title': False, 'related_keyword': row['related_keyword']})
            entry['ids'].append((row['id'], tt))
            if tt == 'add_meta_description':
                entry['need_meta'] = True
            if tt == 'fix_title_length':
                entry['need_title'] = True

    # 2) IA: fetch de contexto + generación batcheada
    urls = list(ai_pending.keys())
    print(f"  {len(urls)} URLs necesitan generación con IA (meta/title)")
    batch = []
    for i, url in enumerate(urls):
        ctx = fetch_page_context(url)
        time.sleep(0.4)
        if not ctx:
            print(f"    ⚠ No se pudo leer {url[:60]}")
            continue
        e = ai_pending[url]
        batch.append({'url': url, 'need_meta': e['need_meta'], 'need_title': e['need_title'],
                      'ctx': ctx, 'related_keyword': e['related_keyword']})
        # procesar batch lleno o al final
        if len(batch) >= GEMINI_BATCH or i == len(urls) - 1:
            if batch:
                print(f"    → Gemini: generando para {len(batch)} páginas...")
                results = generate_ai_values(client, batch) if not dry_run else _dry_preview(batch)
                _apply_ai_results(ai_pending, batch, results, dry_run, stats)
                batch = []

    print(f"\n  Resumen {client['name']}: IA {stats['generated']} · canonical {stats['canonical']} · manual {stats['manual']}")
    return stats


def _dry_preview(batch):
    """En dry-run no llamamos a Gemini; devolvemos placeholders para ver el flujo."""
    return {b['url']: {
        **({'meta_description': '[dry-run meta]'} if b['need_meta'] else {}),
        **({'seo_title': '[dry-run title]'} if b['need_title'] else {}),
    } for b in batch}


def _apply_ai_results(ai_pending, batch, results, dry_run, stats):
    for b in batch:
        url = b['url']
        res = results.get(url, {})
        entry = ai_pending[url]
        for (url_id, tt) in entry['ids']:
            val = None
            if tt == 'add_meta_description':
                val = res.get('meta_description')
            elif tt == 'fix_title_length':
                val = res.get('seo_title')
            if not val:
                continue
            preview = val[:70] + ('…' if len(val) > 70 else '')
            print(f"      [{tt}] {url[:45]} -> {preview}")
            if not dry_run:
                save_suggestion(url_id, suggested_value=val)
            stats['generated'] += 1


def main():
    args = [a for a in sys.argv[1:]]
    dry_run = '--dry-run' in args
    limit = MAX_URLS_PER_RUN
    skip_idx = set()
    if '--limit' in args:
        idx = args.index('--limit')
        try:
            limit = int(args[idx + 1])
            skip_idx.add(idx + 1)  # el valor del limit no es un client_id
        except Exception:
            pass
    client_ids = [a for i, a in enumerate(args) if a.isdigit() and i not in skip_idx]

    if not GEMINI_API_KEY:
        print("ERROR: falta GEMINI_API_KEY")
        sys.exit(1)

    if client_ids:
        for cid in client_ids:
            run_for_client(int(cid), dry_run=dry_run, limit=limit)
    else:
        clients = db.query("SELECT id FROM clients WHERE active = 1 ORDER BY id")
        for c in clients:
            try:
                run_for_client(c['id'], dry_run=dry_run, limit=limit)
            except Exception as e:
                print(f"  ✗ Error cliente {c['id']}: {e}")


if __name__ == '__main__':
    main()