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

POSTS_PER_RUN = int(os.getenv('WRITER_POSTS_PER_RUN', '2'))  # posts/semana por cliente
PUBLISH_WEEKDAY = 1  # 0=lunes ... (compat.)
# Días de publicación dentro de la semana (0=lunes). Con 2 posts/semana → martes y viernes.
PUBLISH_WEEKDAYS = [int(x) for x in os.getenv('WRITER_PUBLISH_WEEKDAYS', '1,4').split(',') if x.strip() != '']
# Horizonte: solo se programa dentro de los próximos N días (evita que las fechas se amontonen
# meses/años al futuro). Si un cliente ya tiene POSTS_PER_RUN posts en esta ventana, no genera más.
NEAR_DAYS = int(os.getenv('WRITER_HORIZON_DAYS', '14'))


# Palabras del nombre del cliente que NO identifican la marca (no se filtran como búsqueda de marca)
BRAND_GENERIC = {
    'grupo', 'grup', 'group', 'global', 'logistics', 'logistica', 'tubes', 'tubos', 'hotel', 'hoteles',
    'iberica', 'fisioterapia', 'calatayud', 'apartments', 'sales', 'consulting', 'servicios',
}


def _norm(text):
    text = (text or '').lower()
    for a, b in {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u', 'ñ': 'n', 'à': 'a', 'è': 'e', 'ò': 'o', 'ç': 'c', 'ï': 'i'}.items():
        text = text.replace(a, b)
    return text


def brand_terms(client):
    """Términos que identifican la marca: palabras del nombre presentes en el dominio + el dominio sin TLD.
    Ej.: 'Marina Camacho' / marinacamacho.com → {'marina', 'camacho', 'marinacamacho'}."""
    host = urlparse(client['domain'] if '//' in (client['domain'] or '') else '//' + (client['domain'] or '')).netloc
    dom = re.sub(r'\.[a-z.]+$', '', host.lower().removeprefix('www.')).replace('-', '')
    terms = {dom} if dom else set()
    for tok in re.findall(r'[a-z0-9]+', _norm(client.get('name'))):
        if len(tok) >= 4 and tok not in BRAND_GENERIC and tok in dom:
            terms.add(tok)
    return terms


def is_brand_query(keyword, terms):
    """True si la búsqueda contiene la marca (palabra suelta o dominio pegado)."""
    k = _norm(keyword)
    words = set(re.findall(r'[a-z0-9]+', k))
    if words & terms or any(len(t) >= 6 and t in k.replace(' ', '') for t in terms):
        return True
    # erratas del dominio completo ("converpack" → convertpack): 1 letra de diferencia, solo dominios largos
    return any(len(t) >= 8 and _lev1(w, t) for t in terms for w in words | {k.replace(' ', '')})


def _lev1(a, b):
    """True si a y b difieren en exactamente una edición (inserción, borrado o sustitución)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)):] == b[i + 1:]



def avoid_terms(client):
    """Temas que el cliente no quiere en el blog (clients.seo_criteria.avoid_topics), normalizados."""
    try:
        crit = json.loads(client.get('seo_criteria') or '{}')
    except Exception:
        crit = {}
    return [t for t in (_norm(x) for x in crit.get('avoid_topics', []) if isinstance(x, str)) if len(t) >= 4]


def hits_avoid(text, terms):
    t = _norm(text or '')
    return next((a for a in terms if a in t), None)


def filter_avoid(opps, terms):
    """Quita de las oportunidades lo que toque un tema excluido y deja la lista a la vista de la IA."""
    if not terms:
        return opps
    keys = {'striking_keywords': 'keyword', 'competitor_topics': 'topic', 'competitor_keyword_gaps': 'keyword', 'seed_keywords': 'keyword'}
    for lst, k in keys.items():
        if opps.get(lst):
            opps[lst] = [x for x in opps[lst] if not hits_avoid(x.get(k, ''), terms)]
    if opps.get('competitor_page_gaps'):
        opps['competitor_page_gaps'] = [x for x in opps['competitor_page_gaps'] if not hits_avoid((x.get('competitor_title') or '') + ' ' + (x.get('gap') or ''), terms)]
    opps['avoid_topics'] = terms
    return opps

def slugify(text):
    text = (text or '').lower().strip()
    reps = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ñ': 'n', 'ü': 'u'}
    for a, b in reps.items():
        text = text.replace(a, b)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text[:90]


def gather_opportunities(client_id, brand=frozenset()):
    """Semillas de temas: keywords striking-distance + temas ganadores de competidores.
    Las búsquedas de marca (`brand`) se descartan: no son temas de blog."""
    striking = db.query("""
        SELECT keyword, ROUND(AVG(position), 1) AS pos, SUM(impressions) AS impr
        FROM metrics_keywords_daily
        WHERE client_id = ? AND date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY keyword
        HAVING pos BETWEEN 8 AND 25 AND impr >= 20
        ORDER BY impr DESC LIMIT 30
    """, [client_id])

    topics = db.query("""
        SELECT topic, opportunity_score, reasoning
        FROM competitor_winning_topics
        WHERE client_id = ?
        ORDER BY opportunity_score DESC LIMIT 12
    """, [client_id])

    # ESPÍA — keyword gaps: keywords por las que un competidor rankea y nosotros mal o nada.
    # Es la señal MÁS fuerte de demanda real no cubierta. Trae la URL del competidor que la
    # gana, su nº de palabras y si usa schema → sirve para superar su cobertura.
    kw_gaps = db.query("""
        SELECT keyword, client_position, competitor_url, competitor_word_count,
               competitor_has_schema, competitor_h1, recommendation
        FROM competitor_keyword_gaps
        WHERE client_id = ?
        ORDER BY client_position DESC, competitor_word_count DESC
        LIMIT 20
    """, [client_id])

    # ESPÍA — pares de páginas: dónde una página de competidor supera a la nuestra equivalente
    # (hueco de contenido), con análisis del gap y recomendación.
    comp_pairs = db.query("""
        SELECT competitor_url, competitor_title, competitor_h1,
               competitor_word_count, client_word_count, gap_analysis, recommendations
        FROM competitor_page_pairs
        WHERE client_id = ?
          AND (COALESCE(gap_analysis,'') <> '' OR COALESCE(recommendations,'') <> '')
        ORDER BY similarity_score DESC
        LIMIT 8
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
        GROUP BY keyword ORDER BY impr DESC LIMIT 40
    """, [client_id])

    striking = [s for s in striking if not is_brand_query(s['keyword'], brand)][:15]
    seed = [s for s in seed if not is_brand_query(s['keyword'], brand)][:20]
    kw_gaps = [g for g in kw_gaps if not is_brand_query(g['keyword'], brand)][:15]

    # Señales de GOOGLE ADS: acciones de conversión y campañas que ya convierten (pistas de
    # intención comercial). Y contexto de GOOGLE ANALYTICS (volumen/conversiones recientes).
    ads_signals, ga4_context = [], []
    try:
        _t = date.today(); _m = _t.month - 3; _y = _t.year
        while _m <= 0: _m += 12; _y -= 1
        since_m = f"{_y:04d}-{_m:02d}"
        acts = db.query("""SELECT action_name, SUM(conversions) c FROM ads_conversions_monthly
            WHERE client_id = ? AND month >= ? GROUP BY action_name ORDER BY c DESC LIMIT 10""", [client_id, since_m])
        camps = db.query("""SELECT campaign_name, SUM(conversions) c, SUM(clicks) k FROM ads_campaigns_monthly
            WHERE client_id = ? AND month >= ? GROUP BY campaign_name ORDER BY c DESC, k DESC LIMIT 10""", [client_id, since_m])
        ads_signals = ([{'accion': a['action_name'], 'conversiones': float(a['c'] or 0)} for a in acts]
                       + [{'campana': c['campaign_name'], 'conversiones': float(c['c'] or 0)} for c in camps])
    except Exception:
        ads_signals = []
    try:
        g = db.query("""SELECT month, sessions, conversions FROM ga4_metrics_monthly
            WHERE client_id = ? ORDER BY month DESC LIMIT 3""", [client_id])
        ga4_context = [{'mes': r['month'], 'sesiones': int(r['sessions'] or 0), 'conversiones': float(r['conversions'] or 0)} for r in g]
    except Exception:
        ga4_context = []

    return {
        'striking_keywords': [{'keyword': s['keyword'], 'position': float(s['pos']), 'impressions': int(s['impr'])} for s in striking],
        'competitor_topics': [{'topic': t['topic'], 'score': int(t['opportunity_score'] or 0), 'why': t['reasoning']} for t in topics],
        'competitor_keyword_gaps': [{
            'keyword': g['keyword'],
            'client_position': float(g['client_position'] or 0),
            'competitor_url': g['competitor_url'],
            'competitor_words': int(g['competitor_word_count'] or 0),
            'competitor_has_schema': bool(g['competitor_has_schema']),
            'recommendation': (g['recommendation'] or '')[:400],
        } for g in kw_gaps],
        'competitor_page_gaps': [{
            'competitor_url': p['competitor_url'],
            'competitor_title': p['competitor_title'],
            'competitor_words': int(p['competitor_word_count'] or 0),
            'our_words': int(p['client_word_count'] or 0),
            'gap': (p['gap_analysis'] or '')[:500],
            'recommendation': (p['recommendations'] or '')[:500],
        } for p in comp_pairs],
        'seed_keywords': [{'keyword': s['keyword'], 'position': float(s['pos'] or 0), 'impressions': int(s['impr'] or 0)} for s in seed],
        'ads_signals': ads_signals,
        'ga4_context': ga4_context,
        'already_covered': [{'title': r['title'], 'keyword': r['target_keyword']} for r in recent],
        'internal_link_targets': internal,
    }


def _taken_dates(client_id):
    """Fechas de publicación ya ocupadas en la ventana cercana (no se pisan)."""
    rows = db.query("""
        SELECT scheduled_for FROM content_drafts
        WHERE client_id = ? AND status <> 'rejected' AND scheduled_for IS NOT NULL
          AND scheduled_for >= CURDATE()
          AND scheduled_for <= DATE_ADD(CURDATE(), INTERVAL ? DAY)
    """, [client_id, NEAR_DAYS + 14])
    taken = set()
    for r in rows:
        try:
            taken.add(date.fromisoformat(str(r['scheduled_for'])[:10]))
        except Exception:
            pass
    return taken


def upcoming_slots(n, taken):
    """n próximas fechas de publicación en PUBLISH_WEEKDAYS a partir de mañana, saltando las ya
    ocupadas. Fechas SIEMPRE cercanas (no se amontonan al futuro); busca hueco hasta ~8 semanas."""
    wds = set(PUBLISH_WEEKDAYS or [PUBLISH_WEEKDAY])
    out, d = [], date.today() + timedelta(days=1)
    for _ in range(60):
        if d.weekday() in wds and d not in taken and d not in out:
            out.append(d)
            if len(out) >= n:
                break
        d += timedelta(days=1)
    return out


def next_weekly_slot(client_id):
    """Siguiente hueco de publicación libre y cercano (compat.)."""
    s = upcoming_slots(1, _taken_dates(client_id))
    return s[0] if s else date.today() + timedelta(days=1)


def weekly_slots(client_id, n):
    """n fechas de publicación cercanas (próximos días en PUBLISH_WEEKDAYS), sin pisar las ya
    programadas del cliente. A diferencia de antes, NO parten del último draft programado, así
    que las fechas no se amontonan meses al futuro."""
    return upcoming_slots(n, _taken_dates(client_id))


def generate_article(client, opps):
    """Genera un artículo completo + SEO a partir de las oportunidades. Devuelve dict o None."""
    brand_list = ', '.join(sorted(brand_terms(client)))
    system_prompt = f"""Eres redactor SEO senior B2B en español para {client['name']} ({client['domain']}).
Tono de marca: {client.get('tone_of_voice') or 'profesional, técnico, orientado a negocio'}.

Elige UN tema de blog que maximice el SEO combinando (ten MUY en cuenta el resultado del ESPÍA de competidores):
- "competitor_keyword_gaps" (ESPÍA — señal MÁS fuerte): keywords por las que un competidor SÍ rankea y nosotros mal o nada. Son demanda real que estamos perdiendo: priorízalas. Cada una trae la URL del competidor que la gana, su nº de palabras ("competitor_words") y si usa schema; úsalo para SUPERAR su cobertura (más profundidad, mejor estructura, schema).
- "competitor_page_gaps" (ESPÍA): páginas de competidores que superan a una nuestra equivalente, con "gap" (qué nos falta) y "recommendation". Úsalas para elegir temas donde podemos recuperar terreno y para saber qué profundidad/ángulo exige el tema (apunta a más palabras que "competitor_words").
- "competitor_topics" (ESPÍA): temas que cubren varios competidores y nosotros no (rellenar huecos), ordenados por "score".
- "striking_keywords": keywords donde ya rankeamos en posición 8-25 (mejorarlas da tráfico rápido).
- "ads_signals": acciones/campañas de Google Ads que YA convierten → pistas de intención COMERCIAL real del cliente (ignora prefijos técnicos como "GA4", "(web)", "click", y nombres de ciudad; quédate con el servicio/producto que sugieren).
- "ga4_context": volumen y conversiones recientes de Analytics (solo contexto para calibrar la ambición del tema; no es una keyword).
Prioriza por este orden: (1) lo que detecta el ESPÍA —competitor_keyword_gaps y competitor_page_gaps— cruzado con (2) lo que YA convierte en Ads; después (3) competitor_topics y striking_keywords. Cuando escribas sobre un gap del espía, cubre el tema con MÁS profundidad y mejor estructura que la página del competidor indicada. NO repitas nada de "already_covered".

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

MARCA: la "focus_keyword" NUNCA puede contener el nombre de la marca ni de la persona ({brand_list}).
Las búsquedas de marca no son temas de blog. Elige una keyword genérica del sector (lo que buscaría alguien
que aún no conoce a {client['name']}) y escríbela en español correcto, con tildes y como la buscaría una persona.
Intégrala en el texto con naturalidad: nunca la metas literal si queda agramatical (p. ej. sin artículo o sin tildes).

Escribe un contenido EXTENSO, EXHAUSTIVO y bien estructurado, listo para publicar
(OBJETIVO: 1300-1900 palabras; nunca menos de 1200) en HTML semántico:
- Solo el cuerpo: <h2>, <h3>, <p>, <ul>/<ol>/<li>, <strong>, y <table> cuando aporte
  (comparativas, tipos, ventajas/inconvenientes, criterios). SIN <h1> (el título va aparte). SIN <html>/<body>.
- EXTENSIÓN Y PROFUNDIDAD: entre 6 y 9 secciones <h2>, varias con subsecciones <h3>.
  Párrafos DESARROLLADOS (3-5 frases cada uno, no frases sueltas). Cubre el tema a fondo:
  contexto/por qué importa, definiciones, tipos o variantes, proceso o cómo hacerlo paso a paso,
  beneficios, errores comunes a evitar, criterios de decisión y casos/aplicaciones reales del sector.
  Prohibido el relleno genérico y repetir la misma idea con otras palabras.
- Si las oportunidades incluyen "avoid_topics", NUNCA elijas ni trates como tema principal nada relacionado con esos términos (son temas que el cliente ha vetado).
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
  "category": "Categoría temática del blog (1-3 palabras) acorde al tema y al sector del cliente; ej. 'Logística', 'Guías', 'Noticias', 'Casos de éxito', 'Normativa'",
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
             meta_description, image_hook, slug, seo_schema, status, scheduled_for, generated_by,
             wp_category_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, 'writer', ?)
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
        (art.get('category') or '')[:150] or None,
    ])


def log_run(client_id, status, summary=None, error=None):
    """Registra la ejecución en agent_runs (para que el panel vea el Redactor activo)."""
    try:
        db.execute("""
            INSERT INTO agent_runs (client_id, agent_name, started_at, finished_at, status, output_summary, error_message)
            VALUES (?, 'writer', NOW(), NOW(), ?, ?, ?)
        """, [client_id, status, json.dumps(summary, ensure_ascii=False) if summary else None, error])
    except Exception:
        pass


def run_for_client(client_id, dry_run=False):
    client = db.query_one("SELECT id, name, domain, tone_of_voice, seo_criteria FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return
    print(f"\n{'='*60}\n  Redactor · {client['name']} ({client['domain']}){'  [DRY-RUN]' if dry_run else ''}\n{'='*60}")

    brand = brand_terms(client)
    opps = gather_opportunities(client_id, brand)
    avoid = avoid_terms(client)
    opps = filter_avoid(opps, avoid)
    if avoid:
        print(f"  Temas excluidos por el cliente: {', '.join(avoid)}")
    # Necesitamos ALGO de lo que tirar: oportunidades, o al menos keywords semilla o páginas reales
    if not (opps['striking_keywords'] or opps['competitor_topics'] or opps.get('competitor_keyword_gaps')
            or opps.get('competitor_page_gaps') or opps.get('seed_keywords') or opps.get('internal_link_targets')):
        print("  ⚠ Sin datos suficientes (ni keywords ni páginas). Ejecuta antes Performance/Auditor/Espía para este cliente.")
        if not dry_run:
            log_run(client_id, 'error', error='Sin datos suficientes')
        return
    fallback = not (opps['striking_keywords'] or opps['competitor_topics']
                    or opps.get('competitor_keyword_gaps') or opps.get('competitor_page_gaps'))
    print(f"  {len(opps['striking_keywords'])} keywords striking · {len(opps['competitor_topics'])} temas competidores · "
          f"ESPÍA {len(opps.get('competitor_keyword_gaps', []))} kw-gaps + {len(opps.get('competitor_page_gaps', []))} page-gaps · "
          f"{len(opps.get('seed_keywords', []))} semilla · {len(opps.get('internal_link_targets', []))} páginas"
          + ("  [modo fallback: sin oportunidades fuertes, escribe a partir de servicios/keywords]" if fallback else ""))

    # TOPE POR COLA: posts cercanos (≤NEAR_DAYS) que el cliente ya tiene en cola (pending_review
    # a punto de publicar + ya programados en WP). Si alcanza POSTS_PER_RUN, no genera más.
    nq = db.query_one("""
        SELECT COUNT(*) n FROM content_drafts
        WHERE client_id = ? AND status <> 'rejected'
          AND scheduled_for >= CURDATE()
          AND scheduled_for <= DATE_ADD(CURDATE(), INTERVAL ? DAY)
    """, [client_id, NEAR_DAYS])
    queued = int((nq or {}).get('n') or 0)
    need = max(0, POSTS_PER_RUN - queued)
    if need == 0:
        print(f"  ⏸  {queued} posts ya en cola (≤{NEAR_DAYS} días); no se generan más esta semana.")
        if not dry_run:
            log_run(client_id, 'success', {'generated': 0, 'skipped': 'cola_llena', 'queued': queued})
        return

    slots = weekly_slots(client_id, need)
    generated = 0
    for i in range(need):
        art = generate_article(client, opps)
        if art and is_brand_query(art.get('focus_keyword', ''), brand):
            print(f"  ⚠ focus_keyword de marca ({art.get('focus_keyword')!r}); se regenera")
            art = generate_article(client, opps)
            if art and is_brand_query(art.get('focus_keyword', ''), brand):
                print("  ✗ La IA insiste en una keyword de marca; se descarta el artículo.")
                continue
        if art and avoid and hits_avoid(f"{art.get('focus_keyword', '')} {art.get('title', '')}", avoid):
            print(f"  ⚠ Tema excluido por el cliente ({hits_avoid(art.get('focus_keyword', '') + ' ' + art.get('title', ''), avoid)!r}); se regenera")
            art = generate_article(client, opps)
            if art and hits_avoid(f"{art.get('focus_keyword', '')} {art.get('title', '')}", avoid):
                print("  ✗ La IA insiste en un tema excluido; se descarta el artículo.")
                continue
        if not art or not art.get('content_html'):
            print("  ✗ No se pudo generar el artículo.")
            continue
        slot = slots[i] if i < len(slots) else next_weekly_slot(client_id)
        save_draft(client_id, art, slot, dry_run=dry_run)
        generated += 1
        # marcar como cubierto en memoria para no repetir en el mismo run
        opps['already_covered'].append({'title': art.get('title'), 'keyword': art.get('focus_keyword')})
    if not dry_run:
        log_run(client_id, 'success', {'generated': generated, 'queued_before': queued, 'target': POSTS_PER_RUN})


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