"""
Agente Redactor — propone contenido de blog basado en datos reales.

Para cada cliente:
  1. Reúne oportunidades: keywords "a distancia de golpe" (Search Console, pos 8-25),
     temas ganadores de competidores (Espía) y contenido ya propuesto (para no repetir).
  2. Elige UN tema y genera un artículo completo en español (HTML con H2/H3), con
     meta description, slug, focus keyword y schema Article (JSON-LD).
  3. Lo programa en el siguiente hueco semanal y lo guarda en content_drafts
     con estado 'pending_review' para que lo revises/edites/publiques desde el dashboard.

NO publica en WordPress: solo genera el borrador para revisión.

Uso:
  python agents/writer.py [client_id] [--dry-run]
"""
import os
import sys
import json
import re
import time
from datetime import date, timedelta
from dotenv import load_dotenv
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry
from gsc_utils import get_gsc_service, fetch_page_queries  # noqa: F401 (por si se amplía)

load_dotenv()
db = DBClient()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-3.5-flash'

POSTS_PER_RUN = int(os.getenv('WRITER_POSTS_PER_RUN', '1'))  # 1 post/semana por cliente
PUBLISH_WEEKDAY = 1  # 0=lunes ... el hueco semanal cae en martes


def slugify(text):
    text = (text or '').lower().strip()
    reps = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ñ': 'n', 'ü': 'u'}
    for a, b in reps.items():
        text = text.replace(a, b)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text[:90]


def gather_opportunities(client_id):
    """Semillas de temas: keywords striking-distance + temas ganadores de competidores."""
    striking = db.query("""
        SELECT keyword, ROUND(AVG(position), 1) AS pos, SUM(impressions) AS impr
        FROM metrics_keywords_daily
        WHERE client_id = ? AND date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY keyword
        HAVING pos BETWEEN 8 AND 25 AND impr >= 20
        ORDER BY impr DESC LIMIT 15
    """, [client_id])

    topics = db.query("""
        SELECT topic, opportunity_score, reasoning
        FROM competitor_winning_topics
        WHERE client_id = ?
        ORDER BY opportunity_score DESC LIMIT 10
    """, [client_id])

    # Contenido ya propuesto (para no repetir), últimos 120 días
    recent = db.query("""
        SELECT title, target_keyword FROM content_drafts
        WHERE client_id = ? AND status <> 'rejected'
          AND generated_at >= DATE_SUB(NOW(), INTERVAL 120 DAY)
    """, [client_id])

    return {
        'striking_keywords': [{'keyword': s['keyword'], 'position': float(s['pos']), 'impressions': int(s['impr'])} for s in striking],
        'competitor_topics': [{'topic': t['topic'], 'score': int(t['opportunity_score'] or 0), 'why': t['reasoning']} for t in topics],
        'already_covered': [{'title': r['title'], 'keyword': r['target_keyword']} for r in recent],
    }


def next_weekly_slot(client_id):
    """Siguiente hueco semanal libre (tras el último draft programado del cliente)."""
    row = db.query_one("""
        SELECT MAX(scheduled_for) AS last FROM content_drafts
        WHERE client_id = ? AND status <> 'rejected' AND scheduled_for IS NOT NULL
    """, [client_id])
    base = date.today()
    if row and row.get('last'):
        try:
            last = date.fromisoformat(str(row['last'])[:10])
            base = max(base, last)
        except Exception:
            pass
    target = base + timedelta(days=7)
    # alinear al día de publicación (martes)
    shift = (PUBLISH_WEEKDAY - target.weekday()) % 7
    return target + timedelta(days=shift)


def generate_article(client, opps):
    """Genera un artículo completo + SEO a partir de las oportunidades. Devuelve dict o None."""
    system_prompt = f"""Eres redactor SEO senior B2B en español para {client['name']} ({client['domain']}).
Tono de marca: {client.get('tone_of_voice') or 'profesional, técnico, orientado a negocio'}.

Elige UN tema de blog que maximice el SEO combinando:
- "striking_keywords": keywords donde ya rankeamos en posición 8-25 (mejorarlas da tráfico rápido).
- "competitor_topics": temas que cubren competidores y nosotros no (rellenar gaps).
Prioriza temas con intención informacional/comercial claros y NO repitas nada de "already_covered".

Escribe un ARTÍCULO COMPLETO listo para publicar (800-1400 palabras) en HTML semántico:
- Solo el cuerpo: <h2>, <h3>, <p>, <ul>/<li>, <strong>. SIN <h1> (el título va aparte). SIN <html>/<body>.
- Estructura clara, útil y específica del sector. Nada de relleno genérico ni inventar datos/cifras.
- Integra de forma natural la keyword principal y variantes reales.

Responde SOLO con JSON válido:
{{
  "title": "Título del post (55-65 caracteres, con la keyword)",
  "focus_keyword": "keyword principal",
  "slug": "slug-url-corto",
  "meta_description": "120-155 caracteres con la keyword y un CTA",
  "content_html": "<h2>...</h2><p>...</p>...",
  "schema": {{
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    "headline": "...",
    "description": "...",
    "keywords": "...",
    "author": {{"@type": "Organization", "name": "{client['name']}"}},
    "publisher": {{"@type": "Organization", "name": "{client['name']}"}}
  }},
  "topic_reason": "1 frase: por qué este tema (keyword/impresiones o gap de competidor)"
}}
"""
    user_prompt = "Oportunidades del cliente:\n" + json.dumps(opps, ensure_ascii=False, indent=2)

    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_prompt)
    data = None
    max_attempts = 3
    for attempt in range(max_attempts):
        raw = generate_with_retry(
            model, user_prompt,
            generation_config={'temperature': 0.6, 'max_output_tokens': 8000, 'response_mime_type': 'application/json'}
        )
        text = raw.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        try:
            data = json.loads(text)
            break
        except Exception as e:
            last = (attempt == max_attempts - 1)
            print(f"  ⚠ JSON inválido{' tras %d intentos' % max_attempts if last else ' (reintento)'}: {e}")
    return data


def save_draft(client_id, art, scheduled_for, dry_run=False):
    slug = art.get('slug') or slugify(art.get('title', ''))
    schema = art.get('schema')
    if isinstance(schema, dict):
        schema['headline'] = schema.get('headline') or art.get('title')
        schema['description'] = schema.get('description') or art.get('meta_description')
    schema_json = json.dumps(schema, ensure_ascii=False) if schema else None

    print(f"  ✍  \"{art.get('title')}\"  → {scheduled_for}  (kw: {art.get('focus_keyword')})")
    print(f"     razón: {art.get('topic_reason', '')}")
    print(f"     {len(art.get('content_html', ''))} car. de contenido · slug: {slug}")
    if dry_run:
        return
    db.execute("""
        INSERT INTO content_drafts
            (client_id, title, content_type, target_keyword, draft_content,
             meta_description, slug, seo_schema, status, scheduled_for, generated_by)
        VALUES (?, ?, 'blog_post', ?, ?, ?, ?, ?, 'pending_review', ?, 'writer')
    """, [
        client_id,
        (art.get('title') or '')[:500],
        (art.get('focus_keyword') or '')[:500],
        art.get('content_html') or '',
        (art.get('meta_description') or '')[:500],
        slug[:200],
        schema_json,
        scheduled_for.isoformat(),
    ])


def run_for_client(client_id, dry_run=False):
    client = db.query_one("SELECT id, name, domain, tone_of_voice FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return
    print(f"\n{'='*60}\n  Redactor · {client['name']} ({client['domain']}){'  [DRY-RUN]' if dry_run else ''}\n{'='*60}")

    opps = gather_opportunities(client_id)
    if not opps['striking_keywords'] and not opps['competitor_topics']:
        print("  ⚠ Sin oportunidades de contenido (faltan datos de Search Console/Espía).")
        return
    print(f"  {len(opps['striking_keywords'])} keywords striking · {len(opps['competitor_topics'])} temas de competidores · "
          f"{len(opps['already_covered'])} ya cubiertos")

    for i in range(POSTS_PER_RUN):
        art = generate_article(client, opps)
        if not art or not art.get('content_html'):
            print("  ✗ No se pudo generar el artículo.")
            continue
        slot = next_weekly_slot(client_id)
        save_draft(client_id, art, slot, dry_run=dry_run)
        # marcar como cubierto en memoria para no repetir en el mismo run
        opps['already_covered'].append({'title': art.get('title'), 'keyword': art.get('focus_keyword')})


def main():
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    client_ids = [a for a in args if a.isdigit()]
    if not GEMINI_API_KEY:
        print("ERROR: falta GEMINI_API_KEY")
        sys.exit(1)
    if client_ids:
        for cid in client_ids:
            run_for_client(int(cid), dry_run=dry_run)
    else:
        clients = db.query("SELECT id FROM clients WHERE active = 1 ORDER BY id")
        for i, c in enumerate(clients):
            try:
                run_for_client(c['id'], dry_run=dry_run)
            except Exception as e:
                print(f"  ✗ Error cliente {c['id']}: {e}")
            if i < len(clients) - 1:
                time.sleep(20)  # espaciar para no saturar el límite por minuto de Gemini


if __name__ == '__main__':
    main()