"""
Agente Auditor - Rastrea las URLs del sitemap del cliente y detecta problemas técnicos.
Guarda hallazgos en audit_findings y snapshots en page_audits.
"""
import os
import sys
import json
import time
import requests
from datetime import date
from urllib.parse import urlparse, urljoin
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()


# === Configuración del crawler ===
USER_AGENT = 'SEO-Agents-Auditor/1.0 (+https://upcofly.es/seo-agents)'
TIMEOUT = 15
MAX_URLS_PER_CLIENT = 500  # Tope por ejecución; ajustable
REQUEST_DELAY = 0.5  # Segundos entre peticiones (cortesía)


def fetch(url, **kwargs):
    """Wrapper de requests con User-Agent y timeout configurados."""
    headers = {'User-Agent': USER_AGENT, **kwargs.pop('headers', {})}
    return requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, **kwargs)


# === Descubrimiento de URLs ===
def discover_urls(domain):
    """Lee el sitemap del cliente y devuelve la lista de URLs."""
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
    """Extrae URLs de un sitemap. Si es un índice, sigue los sub-sitemaps."""
    urls = []
    try:
        # Eliminar namespaces para simplificar el parseo
        content_clean = content
        for ns in ['xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"']:
            content_clean = content_clean.replace(ns, '')
        
        root = ET.fromstring(content_clean)
        
        # Caso 1: sitemap índice (apunta a otros sitemaps)
        for sitemap in root.findall('.//sitemap/loc'):
            sub_url = sitemap.text.strip() if sitemap.text else ''
            if sub_url:
                try:
                    r = fetch(sub_url)
                    if r.status_code == 200:
                        urls.extend(parse_sitemap(sub_url, r.text))
                except requests.RequestException:
                    continue
        
        # Caso 2: sitemap normal (lista de URLs)
        for url in root.findall('.//url/loc'):
            if url.text:
                urls.append(url.text.strip())
    
    except ET.ParseError:
        pass
    
    return urls


# === Análisis de una página ===
def audit_page(url):
    """Visita una URL y extrae métricas técnicas + problemas detectados."""
    start = time.time()
    
    try:
        r = fetch(url)
        elapsed_ms = int((time.time() - start) * 1000)
    except requests.Timeout:
        return {
            'status_code': 0,
            'response_time_ms': TIMEOUT * 1000,
            'findings': [('timeout', 'critical', f'Timeout tras {TIMEOUT}s')]
        }
    except requests.RequestException as e:
        return {
            'status_code': 0,
            'response_time_ms': 0,
            'findings': [('unreachable', 'critical', f'No accesible: {str(e)[:200]}')]
        }
    
    result = {
        'status_code': r.status_code,
        'response_time_ms': elapsed_ms,
        'page_size_bytes': len(r.content),
        'title_length': 0,
        'meta_description_length': 0,
        'h1_count': 0,
        'images_without_alt': 0,
        'internal_links': 0,
        'has_canonical': 0,
        'has_schema': 0,
        'findings': []
    }
    
    # Errores HTTP
    if r.status_code >= 400:
        result['findings'].append(('http_error', 'critical', f'HTTP {r.status_code}'))
        return result
    
    if r.status_code >= 300:
        result['findings'].append(('redirect', 'info', f'HTTP {r.status_code} → redirección'))
    
    # Lentitud
    if elapsed_ms > 3000:
        result['findings'].append(('slow_page', 'warning', f'Tarda {elapsed_ms}ms en cargar (>3s)'))
    elif elapsed_ms > 1500:
        result['findings'].append(('slow_page', 'info', f'Tarda {elapsed_ms}ms en cargar'))
    
    # Tamaño excesivo
    if result['page_size_bytes'] > 2_000_000:
        result['findings'].append(('large_page', 'warning', f'Página pesa {result["page_size_bytes"] // 1024}KB (>2MB)'))
    
    # Solo analizamos HTML
    content_type = r.headers.get('Content-Type', '')
    if 'text/html' not in content_type:
        return result
    
    try:
        soup = BeautifulSoup(r.content, 'lxml')
    except Exception:
        result['findings'].append(('parse_error', 'warning', 'No se pudo parsear el HTML'))
        return result
    
    # Title
    title_tag = soup.find('title')
    if not title_tag or not title_tag.text.strip():
        result['findings'].append(('missing_title', 'critical', 'Falta etiqueta <title>'))
    else:
        title_len = len(title_tag.text.strip())
        result['title_length'] = title_len
        if title_len < 30:
            result['findings'].append(('short_title', 'warning', f'Title muy corto ({title_len} caracteres, ideal 30-60)'))
        elif title_len > 70:
            result['findings'].append(('long_title', 'warning', f'Title muy largo ({title_len} caracteres, ideal 30-60)'))
    
    # Meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if not meta_desc or not meta_desc.get('content', '').strip():
        result['findings'].append(('missing_meta_description', 'warning', 'Falta meta description'))
    else:
        desc_len = len(meta_desc.get('content', '').strip())
        result['meta_description_length'] = desc_len
        if desc_len < 70:
            result['findings'].append(('short_meta_description', 'info', f'Meta description corta ({desc_len} caracteres, ideal 120-160)'))
        elif desc_len > 170:
            result['findings'].append(('long_meta_description', 'warning', f'Meta description muy larga ({desc_len} caracteres, ideal 120-160)'))
    
    # H1
    h1_tags = soup.find_all('h1')
    result['h1_count'] = len(h1_tags)
    if len(h1_tags) == 0:
        result['findings'].append(('missing_h1', 'warning', 'No hay etiqueta H1'))
    elif len(h1_tags) > 1:
        result['findings'].append(('multiple_h1', 'info', f'Hay {len(h1_tags)} etiquetas H1 (recomendable solo 1)'))
    
    # Imágenes sin alt
    images = soup.find_all('img')
    images_without_alt = [img for img in images if not img.get('alt')]
    result['images_without_alt'] = len(images_without_alt)
    if len(images) > 0 and len(images_without_alt) > 0:
        pct = (len(images_without_alt) / len(images)) * 100
        if pct >= 50:
            result['findings'].append(('images_without_alt', 'warning', f'{len(images_without_alt)}/{len(images)} imágenes sin atributo alt ({pct:.0f}%)'))
        elif pct >= 20:
            result['findings'].append(('images_without_alt', 'info', f'{len(images_without_alt)}/{len(images)} imágenes sin atributo alt'))
    
    # Canonical
    canonical = soup.find('link', attrs={'rel': 'canonical'})
    result['has_canonical'] = 1 if canonical and canonical.get('href') else 0
    if not result['has_canonical']:
        result['findings'].append(('missing_canonical', 'info', 'Falta etiqueta canonical'))
    
    # Schema (JSON-LD)
    schema_tags = soup.find_all('script', attrs={'type': 'application/ld+json'})
    result['has_schema'] = 1 if len(schema_tags) > 0 else 0
    if not result['has_schema']:
        result['findings'].append(('missing_schema', 'info', 'Sin schema.org (JSON-LD)'))
    
    # Enlaces internos
    parsed_url = urlparse(url)
    base_domain = parsed_url.netloc
    internal = 0
    for link in soup.find_all('a', href=True):
        href = link['href']
        if href.startswith('/') or base_domain in href:
            internal += 1
    result['internal_links'] = internal
    
    if internal < 3:
        result['findings'].append(('few_internal_links', 'info', f'Solo {internal} enlaces internos detectados'))
    
    return result


# === Persistencia ===
def save_run(client_id, status, input_summary=None, output_summary=None, error=None):
    db.execute("""
        INSERT INTO agent_runs 
            (client_id, agent_name, status, finished_at, input_summary, output_summary, error_message)
        VALUES (?, 'auditor', ?, NOW(), ?, ?, ?)
    """, [
        client_id, status,
        json.dumps(input_summary) if input_summary else None,
        json.dumps(output_summary) if output_summary else None,
        error
    ])


def save_findings(client_id, url, findings):
    """Guarda los hallazgos para una URL. Sin batch por simplicidad (un INSERT por finding)."""
    for issue_type, severity, message in findings:
        db.execute("""
            INSERT INTO audit_findings 
                (client_id, page_url, issue_type, severity, message)
            VALUES (?, ?, ?, ?, ?)
        """, [client_id, url[:1000], issue_type, severity, message])


def save_page_audit(client_id, url, audit_date, audit):
    """Guarda el snapshot técnico de la página."""
    db.execute("""
        INSERT INTO page_audits 
            (client_id, date, url, status_code, response_time_ms, page_size_bytes,
             title_length, meta_description_length, h1_count, images_without_alt,
             internal_links, has_canonical, has_schema)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            status_code = VALUES(status_code),
            response_time_ms = VALUES(response_time_ms),
            page_size_bytes = VALUES(page_size_bytes),
            title_length = VALUES(title_length),
            meta_description_length = VALUES(meta_description_length),
            h1_count = VALUES(h1_count),
            images_without_alt = VALUES(images_without_alt),
            internal_links = VALUES(internal_links),
            has_canonical = VALUES(has_canonical),
            has_schema = VALUES(has_schema)
    """, [
        client_id, audit_date, url[:1000],
        audit.get('status_code', 0),
        audit.get('response_time_ms', 0),
        audit.get('page_size_bytes', 0),
        audit.get('title_length', 0),
        audit.get('meta_description_length', 0),
        audit.get('h1_count', 0),
        audit.get('images_without_alt', 0),
        audit.get('internal_links', 0),
        audit.get('has_canonical', 0),
        audit.get('has_schema', 0)
    ])


# === Ejecución por cliente ===
def run_for_client(client):
    print(f"\n→ Auditando: {client['name']} ({client['domain']})")
    
    try:
        # 1. Descubrir URLs
        print(f"  Buscando sitemap...")
        urls = discover_urls(client['domain'])
        
        if not urls:
            print(f"  ⚠ No se encontró sitemap accesible")
            save_run(client['id'], 'error', error='No sitemap found')
            return
        
        urls = list(set(urls))  # Quitar duplicados
        urls_to_audit = urls[:MAX_URLS_PER_CLIENT]
        print(f"  Encontradas {len(urls)} URLs en sitemap. Auditando {len(urls_to_audit)} (tope: {MAX_URLS_PER_CLIENT})")
        
        # 2. Auditar cada URL
        audit_date = date.today().isoformat()
        total_findings = 0
        critical = 0
        warnings = 0
        
        for i, url in enumerate(urls_to_audit, 1):
            audit = audit_page(url)
            
            # Guardar snapshot técnico
            save_page_audit(client['id'], url, audit_date, audit)
            
            # Guardar findings
            findings = audit.get('findings', [])
            if findings:
                save_findings(client['id'], url, findings)
                total_findings += len(findings)
                critical += sum(1 for f in findings if f[1] == 'critical')
                warnings += sum(1 for f in findings if f[1] == 'warning')
            
            if i % 20 == 0:
                print(f"    {i}/{len(urls_to_audit)} URLs procesadas...")
            
            time.sleep(REQUEST_DELAY)
        
        print(f"  ✓ Auditadas {len(urls_to_audit)} URLs")
        print(f"  ✓ Hallazgos: {total_findings} ({critical} críticos, {warnings} warnings)")
        
        save_run(
            client['id'], 'success',
            input_summary={'urls_found': len(urls), 'urls_audited': len(urls_to_audit)},
            output_summary={'findings': total_findings, 'critical': critical, 'warnings': warnings}
        )
    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        try:
            save_run(client['id'], 'error', error=str(e))
        except Exception as inner:
            print(f"  ⚠ No se pudo registrar el error: {inner}")
        raise


def main():
    clients = db.query("SELECT id, domain, name FROM clients WHERE active = 1")
    print(f"Encontrados {len(clients)} clientes activos para auditar")
    
    for client in clients:
        try:
            run_for_client(client)
        except Exception:
            continue


if __name__ == '__main__':
    main()