"""
Agente Estratega - Lee todos los datos del cliente y genera tareas SEO priorizadas usando Gemini.
Soporta modo masivo (todos los clientes activos) y modo bajo demanda (un cliente concreto).
Vincula URLs concretas afectadas a cada tarea técnica.
"""
import os
import sys
import json
import re
import time
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient
from gemini_utils import generate_with_retry, PACING_STRATEGIST

load_dotenv()
db = DBClient()


GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    print("ERROR: Falta GEMINI_API_KEY en .env o en secrets")
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-3.5-flash'


LOW_RISK_TYPES = {
    'add_meta_description',
    'fix_title_length',
    'add_alt_to_images',
    'add_canonical',
    'add_schema',
    'fix_h1_count'
}


# Mapeo task_type → issue_types del Auditor (para vincular URLs)
TYPE_TO_ISSUE = {
    'add_meta_description': ['missing_meta_description', 'short_meta_description'],
    'fix_title_length': ['short_title', 'long_title', 'missing_title'],
    'add_alt_to_images': ['images_without_alt'],
    'add_canonical': ['missing_canonical'],
    'add_schema': ['missing_schema'],
    'fix_h1_count': ['missing_h1', 'multiple_h1']
}


def build_client_dossier(client_id):
    """Reúne todos los datos relevantes del cliente para alimentar a Gemini."""
    dossier = {}
    
    client = db.query_one("""
        SELECT id, domain, name, tone_of_voice, seo_criteria
        FROM clients WHERE id = ?
    """, [client_id])
    
    if not client:
        return None
    
    dossier['client'] = {
        'name': client['name'],
        'domain': client['domain'],
        'tone_of_voice': client['tone_of_voice'],
        'seo_criteria': json.loads(client['seo_criteria']) if client['seo_criteria'] else {}
    }
    
    summary = db.query_one("""
        SELECT 
            COALESCE(SUM(total_clicks), 0) AS clicks,
            COALESCE(SUM(total_impressions), 0) AS impressions,
            AVG(avg_position) AS avg_position
        FROM metrics_daily
        WHERE client_id = ?
          AND date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
    """, [client_id])
    
    dossier['performance_7d'] = {
        'clicks': int(summary['clicks'] or 0),
        'impressions': int(summary['impressions'] or 0),
        'avg_position': round(float(summary['avg_position'] or 0), 1)
    }
    
    opportunities = db.query("""
        SELECT 
            keyword,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            ROUND(AVG(position), 1) AS position
        FROM metrics_keywords_daily
        WHERE client_id = ?
          AND date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        GROUP BY keyword
        HAVING position BETWEEN 5 AND 15 AND impressions >= 30
        ORDER BY impressions DESC
        LIMIT 15
    """, [client_id])
    
    dossier['opportunities'] = [
        {
            'keyword': o['keyword'],
            'impressions': int(o['impressions']),
            'clicks': int(o['clicks']),
            'position': float(o['position'])
        } for o in opportunities
    ]
    
    top_pages = db.query("""
        SELECT 
            url,
            SUM(clicks) AS clicks,
            SUM(impressions) AS impressions,
            ROUND(AVG(position), 1) AS position
        FROM metrics_pages_daily
        WHERE client_id = ?
          AND date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        GROUP BY url
        ORDER BY clicks DESC
        LIMIT 10
    """, [client_id])
    
    dossier['top_pages'] = [
        {
            'url': p['url'],
            'clicks': int(p['clicks']),
            'impressions': int(p['impressions']),
            'position': float(p['position'])
        } for p in top_pages
    ]
    
    findings_summary = db.query("""
        SELECT 
            issue_type,
            severity,
            COUNT(*) AS total
        FROM audit_findings
        WHERE client_id = ? AND resolved_at IS NULL
        GROUP BY issue_type, severity
        ORDER BY FIELD(severity, 'critical', 'warning', 'info'), total DESC
    """, [client_id])
    
    dossier['technical_findings_summary'] = [
        {
            'issue_type': f['issue_type'],
            'severity': f['severity'],
            'pages_affected': int(f['total'])
        } for f in findings_summary
    ]
    
    critical_pages = db.query("""
        SELECT 
            page_url,
            issue_type,
            message
        FROM audit_findings
        WHERE client_id = ? 
          AND severity = 'critical'
          AND resolved_at IS NULL
        ORDER BY detected_at DESC
        LIMIT 10
    """, [client_id])
    
    dossier['critical_pages_examples'] = [
        {
            'url': p['page_url'],
            'issue': p['issue_type'],
            'message': p['message']
        } for p in critical_pages
    ]
    
    previous_tasks = db.query("""
        SELECT title, task_type, status
        FROM tasks
        WHERE client_id = ?
          AND generated_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        ORDER BY generated_at DESC
        LIMIT 20
    """, [client_id])
    
    dossier['recent_tasks'] = [
        {
            'title': t['title'],
            'type': t['task_type'],
            'status': t['status']
        } for t in previous_tasks
    ]
    
    return dossier


SYSTEM_PROMPT = """Eres un consultor SEO senior con 15 años de experiencia trabajando con webs B2B en español.

Tu trabajo es analizar los datos de un cliente y generar entre 5 y 10 tareas SEO priorizadas y muy concretas para mejorar su posicionamiento orgánico.

# Criterios para generar tareas

1. Sé específico: nunca propongas "mejorar el SEO" o "optimizar contenido". Cada tarea debe ser una acción concreta sobre una página o keyword concreta.

2. Prioriza por impacto/esfuerzo:
   - Las tareas técnicas suelen ser de bajo esfuerzo y alto impacto (sobre todo si afectan muchas páginas)
   - Las oportunidades de keywords en posición 5-15 son las más rentables (esfuerzo medio, impacto alto)
   - Crear contenido nuevo es alto esfuerzo, déjalo para casos claros

3. Respeta el tono del cliente: si el cliente tiene un tono "B2B industrial técnico", no propongas tareas con tono casual.

4. No repitas tareas recientes: si ves una tarea similar en "recent_tasks", no la propongas otra vez a menos que esté rechazada.

5. Razona cada tarea: en el campo "reasoning" explica brevemente POR QUÉ esa tarea importa para este cliente concreto, no de forma genérica.

# Tipos de tareas válidas

Tareas técnicas (bajo riesgo, se auto-aprueban):
- add_meta_description: añadir meta description a páginas que no tienen
- fix_title_length: corregir titles muy cortos o muy largos
- add_alt_to_images: añadir atributos alt a imágenes
- add_canonical: añadir etiqueta canonical donde falta
- add_schema: añadir schema.org básico
- fix_h1_count: corregir páginas sin H1 o con múltiples H1

Tareas de optimización (revisión humana):
- optimize_for_keyword: optimizar una página existente para una keyword en posición 5-15
- improve_internal_linking: mejorar enlazado interno hacia una página
- rewrite_content: reescribir contenido existente

Tareas de contenido (revisión humana):
- create_new_content: crear nueva página/artículo para una keyword sin contenido propio
- expand_content: ampliar contenido existente que es muy corto

Tareas estratégicas (revisión humana):
- consolidate_pages: fusionar páginas que canibalizan
- redirect_obsolete: redirigir páginas obsoletas

# Formato de respuesta

DEBES responder EXCLUSIVAMENTE con un JSON válido.

Estructura exacta:

{
  "analysis_summary": "Resumen ejecutivo del estado del cliente en 2-3 frases concretas con números reales",
  "tasks": [
    {
      "task_type": "add_meta_description",
      "category": "technical",
      "priority": "high",
      "title": "Añadir meta description a 47 páginas del cliente",
      "description": "Detectadas 47 páginas sin meta description. Generar descripciones únicas de 140-160 caracteres que incluyan la keyword principal y un CTA.",
      "reasoning": "Las páginas afectadas representan el 28% del sitio y algunas reciben 100+ impresiones/semana sin meta optimizada, lo que reduce su CTR esperado.",
      "target_url": null,
      "related_keyword": null
    }
  ]
}

Reglas estrictas del JSON:
- "priority" solo puede ser: "critical", "high", "medium", "low"
- "category" solo puede ser: "technical", "content", "optimization", "strategy"
- Si "target_url" no aplica, usar null
- Si "related_keyword" no aplica, usar null
- Generar entre 5 y 10 tareas, ni más ni menos
"""


def try_repair_json(text):
    """Intenta reparar un JSON truncado cerrando tareas incompletas."""
    last_complete_task = text.rfind('    }')
    if last_complete_task == -1:
        return None
    truncated = text[:last_complete_task + len('    }')]
    repaired = truncated + '\n  ]\n}'
    return repaired


def generate_tasks(dossier):
    """Llama a Gemini con streaming para feedback visual y devuelve tareas estructuradas."""
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_PROMPT
    )
    
    user_prompt = f"""Aquí están los datos del cliente. Genera entre 5 y 10 tareas priorizadas.

Datos del cliente:
{json.dumps(dossier, indent=2, ensure_ascii=False)}

Responde SOLO con el JSON estructurado."""
    
    print("  ", end='', flush=True)
    progress = {'last_dot': 0}

    def on_progress(chars_received):
        while progress['last_dot'] + 100 <= chars_received:
            print('.', end='', flush=True)
            progress['last_dot'] += 100

    raw_text = generate_with_retry(
        model,
        user_prompt,
        generation_config={
            'temperature': 0.3,
            'max_output_tokens': 8000,
            'response_mime_type': 'application/json'
        },
        on_progress=on_progress
    )

    print(f" [{len(raw_text)} caracteres recibidos]")
    
    raw_text = raw_text.strip()
    
    if raw_text.startswith('```'):
        raw_text = re.sub(r'^```(?:json)?\n?', '', raw_text)
        raw_text = re.sub(r'\n?```$', '', raw_text)
    
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"  ⚠ Error parseando JSON: {e}")
        print(f"  Intentando reparar JSON truncado...")
        
        repaired = try_repair_json(raw_text)
        if repaired:
            try:
                result = json.loads(repaired)
                num_tasks = len(result.get('tasks', []))
                print(f"  ✓ JSON reparado: rescatadas {num_tasks} tareas")
                return result
            except json.JSONDecodeError:
                pass
        
        print(f"  ✗ No se pudo reparar. Respuesta cruda: {raw_text[:500]}")
        return None


def link_affected_urls(task_id, client_id, task_type):
    """Vincula las URLs afectadas a una tarea técnica basándose en los hallazgos del Auditor."""
    if task_type not in TYPE_TO_ISSUE:
        return 0
    
    issue_types = TYPE_TO_ISSUE[task_type]
    placeholders = ','.join(['?'] * len(issue_types))
    
    affected = db.query(f"""
        SELECT 
            af.id AS finding_id,
            af.page_url,
            af.message,
            pa.title_length,
            pa.meta_description_length,
            pa.h1_count,
            pa.images_without_alt,
            pa.has_canonical,
            pa.has_schema
        FROM audit_findings af
        LEFT JOIN page_audits pa ON pa.client_id = af.client_id 
            AND pa.url = af.page_url
            AND pa.date = (SELECT MAX(date) FROM page_audits WHERE client_id = af.client_id)
        WHERE af.client_id = ? 
            AND af.issue_type IN ({placeholders})
            AND af.resolved_at IS NULL
        ORDER BY af.severity, af.detected_at DESC
        LIMIT 200
    """, [client_id] + issue_types)
    
    linked = 0
    seen_urls = set()  # evita duplicar la misma URL (los hallazgos se acumulan entre auditorías)
    for aff in affected:
        if aff['page_url'] in seen_urls:
            continue
        seen_urls.add(aff['page_url'])
        if task_type == 'fix_title_length':
            current_value = f"Title actual: {aff.get('title_length', 0)} caracteres"
        elif task_type == 'add_meta_description':
            current_value = f"Meta actual: {aff.get('meta_description_length', 0)} caracteres"
        elif task_type == 'fix_h1_count':
            current_value = f"H1 actual: {aff.get('h1_count', 0)} etiquetas"
        elif task_type == 'add_alt_to_images':
            current_value = f"Imágenes sin alt: {aff.get('images_without_alt', 0)}"
        elif task_type == 'add_canonical':
            current_value = "Falta canonical"
        elif task_type == 'add_schema':
            current_value = "Falta schema.org"
        else:
            current_value = aff.get('message', '')
        
        try:
            db.execute("""
                INSERT INTO task_affected_urls 
                    (task_id, page_url, current_value, finding_id, url_status)
                VALUES (?, ?, ?, ?, 'pending')
            """, [
                task_id,
                aff['page_url'][:1000],
                current_value,
                aff['finding_id']
            ])
            linked += 1
        except Exception as e:
            print(f"    ⚠ Error vinculando URL: {e}")
    
    return linked


def save_tasks(client_id, result):
    """Guarda las tareas generadas. Auto-aprueba las de bajo riesgo y vincula URLs afectadas."""
    if not result or 'tasks' not in result:
        return 0, 0
    
    saved = 0
    auto_approved = 0
    
    for task in result.get('tasks', []):
        task_type = task.get('task_type', 'unknown')
        category = task.get('category', 'optimization')
        priority = task.get('priority', 'medium')
        
        is_low_risk = task_type in LOW_RISK_TYPES and category == 'technical'
        status = 'approved' if is_low_risk else 'pending_review'
        auto_approve_flag = 1 if is_low_risk else 0
        
        if is_low_risk:
            auto_approved += 1
        
        db.execute("""
            INSERT INTO tasks 
                (client_id, task_type, category, priority, 
                 title, description, reasoning,
                 target_url, related_keyword,
                 status, auto_approved, generated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'strategist')
        """, [
            client_id,
            task_type,
            category,
            priority,
            task.get('title', '')[:500],
            task.get('description', ''),
            task.get('reasoning', ''),
            task.get('target_url'),
            task.get('related_keyword'),
            status,
            auto_approve_flag
        ])
        
        # Obtener el ID de la tarea recién creada
        task_id_row = db.query_one("""
            SELECT id FROM tasks 
            WHERE client_id = ? AND task_type = ? 
            ORDER BY id DESC LIMIT 1
        """, [client_id, task_type])
        
        if task_id_row:
            task_id = task_id_row['id']
            # Vincular URLs afectadas si es una tarea técnica conocida
            linked = link_affected_urls(task_id, client_id, task_type)
            if linked > 0:
                print(f"    ✓ Tarea {task_id} ({task_type}): {linked} URLs vinculadas")
        
        saved += 1
    
    return saved, auto_approved


def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs 
            (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'strategist', ?, NOW(), ?, ?, ?)
    """, [
        client_id, status,
        json.dumps(input_summary) if input_summary else None,
        json.dumps(output_summary) if output_summary else None,
        error
    ])


def run_for_client(client_id):
    """Genera tareas para un cliente concreto."""
    client = db.query_one("SELECT name, domain FROM clients WHERE id = ?", [client_id])
    if not client:
        print(f"  ⚠ Cliente {client_id} no encontrado")
        return
    
    print(f"\n→ Analizando: {client['name']} ({client['domain']})")
    
    try:
        print(f"  Construyendo expediente...")
        dossier = build_client_dossier(client_id)
        
        if not dossier:
            print(f"  ⚠ No se pudo construir el expediente")
            save_run(client_id, 'error', error='Could not build dossier')
            return
        
        data_summary = {
            'opportunities': len(dossier.get('opportunities', [])),
            'top_pages': len(dossier.get('top_pages', [])),
            'findings_types': len(dossier.get('technical_findings_summary', [])),
            'previous_tasks': len(dossier.get('recent_tasks', []))
        }
        print(f"  Datos: {data_summary}")
        
        print(f"  Consultando a Gemini...")
        result = generate_tasks(dossier)
        
        if not result:
            save_run(client_id, 'error', input_summary=data_summary, error='Failed to generate tasks')
            return
        
        saved, auto_approved = save_tasks(client_id, result)
        print(f"  ✓ {saved} tareas generadas ({auto_approved} auto-aprobadas, {saved - auto_approved} pendientes de revisión)")
        
        save_run(
            client_id, 'success',
            input_summary=data_summary,
            output_summary={
                'tasks_generated': saved,
                'auto_approved': auto_approved,
                'pending_review': saved - auto_approved,
                'analysis_summary': result.get('analysis_summary', '')[:500]
            }
        )
    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        try:
            save_run(client_id, 'error', error=str(e))
        except Exception:
            pass
        raise


def main():
    if len(sys.argv) > 1:
        client_id = int(sys.argv[1])
        print(f"Modo bajo demanda: cliente ID {client_id}")
        run_for_client(client_id)
    else:
        clients = db.query("SELECT id FROM clients WHERE active = 1")
        print(f"Modo masivo: {len(clients)} clientes activos")
        for i, client in enumerate(clients):
            try:
                run_for_client(client['id'])
            except Exception:
                continue
            # Pausa entre clientes para no saturar el límite de 5 req/min de Gemini
            if i < len(clients) - 1:
                print(f"  ⏸  Pausa de {PACING_STRATEGIST}s antes del siguiente cliente...")
                time.sleep(PACING_STRATEGIST)


if __name__ == '__main__':
    main()