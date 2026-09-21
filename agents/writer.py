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
from urllib.parse import urlparse
from dotenv import load_dotenv
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry
from gsc_utils import get_gsc_service, fetch_page_queries  # noqa: F401 (por si se amplía)

try:
    from google.api_core.exceptions import ResourceExhausted
except Exception:  # pragma: no cover
    class ResourceExhausted(Exception):
        pass

load_dotenv()
db = DBClient()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
# El Redactor puede usar un modelo propio (WRITER_MODEL) para contenido más largo/rico;
# por defecto respeta GEMINI_MODEL. Sugerido para artículos extensos: gemini-flash-latest.
MODEL_NAME = os.getenv('WRITER_MODEL') or os.getenv('GEMINI_MODEL', 'gemini-flash-lite-latest')

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

    # Páginas reales del cliente (con tráfico) para el enlazado interno
    pages = db.query("""
        SELECT url, SUM(impressions) AS impr
        FROM metrics_pages_daily
        WHERE client_id = ? AND date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
        GROUP BY url ORDER BY impr DESC LIMIT 60
    """, [client_id])
    seen, internal = set(), []
    for r in pages:
        path = urlparse(r['url']).path or '/'
        if path in seen or path in ('/', '/carrito/', '/checkout/', '/cart/'):
            continue
        seen.add(path)
        topic = path.strip('/').replace('-', ' ').replace('/', ' › ')
        internal.append({'url': r['url'], 'topic': topic})
        if len(internal) >= 25:
            break

    # Keywords semilla: TODAS las del cliente por impresiones (sin filtro), para clientes con poco tráfico
    seed = db.query("""
        SELECT keyword, ROUND(AVG(position), 1) AS pos, SUM(impressions) AS impr
        FROM metrics_keywords_daily
        WHERE client_id = ? AND date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
        GROUP BY keyword ORDER BY impr DESC LIMIT 20
    """, [client_id])

    return {
        'striking_keywords': [{'keyword': s['keyword'], 'position': float(s['pos']), 'impressions': int(s['impr'])} for s in striking],
        'competitor_topics': [{'topic': t['topic'], 'score': int(t['opportunity_score'] or 0), 'why': t['reasoning']} for t in topics],
        'seed_keywords': [{'keyword': s['keyword'], 'position': float(s['pos'] or 0), 'impressions': int(s['impr'] or 0)} for s in seed],
        'already_covered': [{'title': r['title'], 'keyword': r['target_keyword']} for r in recent],
        'internal_link_targets': internal,
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

Si NO hay "striking_keywords" ni "competitor_topics" (cliente con poco tráfico), ELIGE IGUALMENTE un tema
útil y relevante para el negocio a partir de "seed_keywords" (búsquedas reales del cliente, aunque tengan
pocas impresiones) y sobre todo de "internal_link_targets" (sus servicios y páginas REALES). Deduce el sector
y los servicios de esas páginas. NUNCA inventes servicios que la empresa no ofrezca. SIEMPRE debes proponer un
artículo (nunca respondas vacío).

Primero DECIDE el tipo de contenido ("content_type"):
- "post": artículo informativo de blog (guías, "qué es", "cómo", tendencias). Ideal para
  intención informacional y para keywords/temas educativos.
- "landing": página de servicio orientada a conversión (describe un servicio/solución que
  ofrece la empresa, con beneficios, proceso, aplicaciones y CTA claro). Ideal cuando la
  oportunidad es comercial/transaccional o el competidor tiene una landing de ese servicio.

Escribe un contenido EXTENSO, EXHAUSTIVO y bien estructurado, listo para publicar
(OBJETIVO: 1300-1900 palabras; nunca menos de 1200) en HTML semántico:
- Solo el cuerpo: <h2>, <h3>, <p>, <ul>/<ol>/<li>, <strong>, y <table> cuando aporte
  (comparativas, tipos, ventajas/inconvenientes, criterios). SIN <h1> (el título va aparte). SIN <html>/<body>.
- EXTENSIÓN Y PROFUNDIDAD: entre 6 y 9 secciones <h2>, varias con subsecciones <h3>.
  Párrafos DESARROLLADOS (3-5 frases cada uno, no frases sueltas). Cubre el tema a fondo:
  contexto/por qué importa, definiciones, tipos o variantes, proceso o cómo hacerlo paso a paso,
  beneficios, errores comunes a evitar, criterios de decisión y casos/aplicaciones reales del sector.
  Prohibido el relleno genérico y repetir la misma idea con otras palabras.
- INTRODUCCIÓN de 2-3 párrafos que enganche, plantee el problema y adelante qué va a aprender el lector.
- Incluye AL MENOS UNA lista (<ul> u <ol>) y, cuando sea pertinente, AL MENOS UNA <table>
  (p.ej. comparativa de opciones, tipos, o ventajas frente a inconvenientes).
- COBERTURA SEMÁNTICA SEO: integra de forma natural la keyword principal, sus variantes reales
  y términos/entidades relacionados del sector (amplía el campo semántico), sin sobreoptimizar.
- Detalle concreto y específico del sector (procesos, ejemplos, criterios). NO inventes datos,
  cifras, estudios ni normativas; si citas una cifra debe ser de conocimiento general y verificable.
- Si es "landing": misma extensión, estructura orientada a venta (propuesta de valor, beneficios,
  proceso/cómo trabajamos, aplicaciones/sectores, diferenciación/garantías y cierre con CTA claro).
- Si es "post": guía informativa completa que resuelva POR ENTERO la intención de búsqueda.
- ENLAZADO INTERNO OBLIGATORIO: incluye 4-6 enlaces internos usando
  <a href="URL">texto ancla descriptivo</a> hacia las páginas más relacionadas de
  "internal_link_targets". Usa EXCLUSIVAMENTE esas URLs reales (nunca inventes rutas),
  con ancla natural que describa el destino (nada de "haz clic aquí"), repartidos en
  distintas secciones y sin repetir la misma URL.
- CONCLUSIÓN final (1-2 párrafos) que resuma y cierre con un CTA hacia un servicio/página real.
- FAQ OBLIGATORIA (mejora la visibilidad en IA): como última sección, un
  <h2>Preguntas frecuentes</h2> con 4-6 preguntas reales del sector, cada una con su
  <h3>¿pregunta?</h3> seguida de un <p> con respuesta directa y útil (2-4 frases).
  Las mismas preguntas/respuestas deben ir también en el schema FAQPage.

Responde SOLO con JSON válido:
{{
  "content_type": "post" | "landing",
  "title": "Título (55-65 caracteres, con la keyword)",
  "hook": "Frase gancho de 5-9 palabras para la imagen destacada (impactante, directa, sin comillas ni punto final)",
  "focus_keyword": "keyword principal",
  "slug": "slug-url-corto",
  "meta_description": "120-155 caracteres con la keyword y un CTA",
  "content_html": "<h2>...</h2><p>...</p>...",
  "schema": {{
    "@context": "https://schema.org",
    "@graph": [
      {{
        "@type": "BlogPosting (si post) o Service (si landing)",
        "name": "...", "description": "...", "keywords": "...",
        "provider": {{"@type": "Organization", "name": "{client['name']}"}}
      }},
      {{
        "@type": "FAQPage",
        "mainEntity": [
          {{"@type": "Question", "name": "¿pregunta?", "acceptedAnswer": {{"@type": "Answer", "text": "respuesta directa"}}}}
        ]
      }}
    ]
  }},
  "topic_reason": "1 frase: por qué este tema y tipo (keyword/impresiones o gap de competidor)"
}}
"""
    user_prompt = "Oportunidades del cliente:\n" + json.dumps(opps, ensure_ascii=False, indent=2)

    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_prompt)
    data = None
    max_attempts = 3
    for attempt in range(max_attempts):
        raw = generate_with_retry(
            model, user_prompt,
            generation_config={'temperature': 0.65, 'max_output_tokens': 16000, 'response_mime_type': 'application/json'}
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

    ctype = art.get('content_type', 'post')
    if ctype not in ('post', 'landing'):
        ctype = 'post'

    print(f"  ✍  [{ctype.upper()}] \"{art.get('title')}\"  → {scheduled_for}  (kw: {art.get('focus_keyword')})")
    print(f"     razón: {art.get('topic_reason', '')}")
    print(f"     {len(art.get('content_html', ''))} car. de contenido · slug: {slug}")
    if dry_run:
        return
    db.execute("""
        INSERT INTO content_drafts
            (client_id, title, content_type, target_keyword, draft_content,
             meta_description, image_hook, slug, seo_schema, status, scheduled_for, generated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, 'writer')
    """, [
        client_id,
        (art.get('title') or '')[:500],
        ctype,
        (art.get('focus_keyword') or '')[:500],
        art.get('content_html') or '',
        (art.get('meta_description') or '')[:500],
        (art.get('hook') or art.get('title') or '')[:255],
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
    # Necesitamos ALGO de lo que tirar: oportunidades, o al menos keywords semilla o páginas reales
    if not (opps['striking_keywords'] or opps['competitor_topics'] or opps.get('seed_keywords') or opps.get('internal_link_targets')):
        print("  ⚠ Sin datos suficientes (ni keywords ni páginas). Ejecuta antes Performance/Auditor para este cliente.")
        return
    fallback = not opps['striking_keywords'] and not opps['competitor_topics']
    print(f"  {len(opps['striking_keywords'])} keywords striking · {len(opps['competitor_topics'])} temas de competidores · "
          f"{len(opps.get('seed_keywords', []))} keywords semilla · {len(opps.get('internal_link_targets', []))} páginas"
          + ("  [modo fallback: sin oportunidades fuertes, escribe a partir de servicios/keywords]" if fallback else ""))

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
        try:
            for cid in client_ids:
                run_for_client(int(cid), dry_run=dry_run)
        except ResourceExhausted:
            print("\n⏸  Cuota diaria de Gemini agotada. (Salida OK, reanudará en la próxima ejecución.)")
            return
    else:
        clients = db.query("SELECT id FROM clients WHERE active = 1 AND content_enabled = 1 ORDER BY id")
        if not clients:
            print("Sin clientes con 'Generar publicaciones' activado.")
        for i, c in enumerate(clients):
            try:
                run_for_client(c['id'], dry_run=dry_run)
            except ResourceExhausted:
                print("\n⏸  Cuota diaria de Gemini agotada. Lo generado se ha guardado; "
                      "el resto se reanudará mañana. (Salida OK, no es un fallo.)")
                break
            except Exception as e:
                print(f"  ✗ Error cliente {c['id']}: {e}")
            if i < len(clients) - 1:
                time.sleep(20)  # espaciar para no saturar el límite por minuto de Gemini


if __name__ == '__main__':
    main()