#!/usr/bin/env python3
"""
Conector SEO para tiendas PrestaShop (Italifters).

Lee productos por el Webservice API de PrestaShop A TRAVÉS del Cloudflare Worker
(ruta /ps/), porque el WAF openresty de la tienda bloquea el acceso directo desde
IPs de automatización (HTTP 415). Cloudflare sí pasa.

Flujo por producto:
  1. GET  /ps/products/{id}?display=full   -> XML del producto (name + meta actuales)
  2. Si la meta es débil (vacía / demasiado corta o larga) genera meta_title y
     meta_description con Gemini a partir del nombre y contexto del sector.
  3. (solo con --apply) PUT /ps/products/{id} con el XML modificado (solo se tocan
     los CDATA de meta_title / meta_description del idioma por defecto).

Por defecto es DRY-RUN: imprime antes/después y NO escribe nada.

Uso:
  python agents/prestashop.py                 # dry-run, primeros PS_MAX productos
  python agents/prestashop.py --apply         # aplica de verdad
  python agents/prestashop.py --limit 5       # nº de productos
  python agents/prestashop.py --ids 21,22     # productos concretos

Env:
  WORKER_URL      (def. https://seo-agents-proxy.david-0bd.workers.dev)
  PS_PROXY_TOKEN  token X-PS-Token compartido con el Worker (obligatorio)
  GEMINI_API_KEY
"""
import os
import re
import sys
import html
import json
import time

import requests
from dotenv import load_dotenv
import google.generativeai as genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gemini_utils import generate_with_retry

try:
    from google.api_core.exceptions import ResourceExhausted
except Exception:  # pragma: no cover
    class ResourceExhausted(Exception):
        pass

load_dotenv()

WORKER_URL = os.getenv('WORKER_URL', 'https://seo-agents-proxy.david-0bd.workers.dev').rstrip('/')
PS_TOKEN   = os.getenv('PS_PROXY_TOKEN', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
MODEL_NAME = 'gemini-3.5-flash'
DEFAULT_LANG = os.getenv('PS_LANG', '1')          # id de idioma por defecto (1 = español)
PS_MAX = int(os.getenv('PS_MAX', '25'))           # productos por ejecución
PACING = float(os.getenv('PS_PACING', '2'))       # segundos entre productos

BRAND = 'Italifters'
SECTOR = ('herramientas profesionales para levantar/abrir tapas de arqueta y registro '
          '(sectores: bomberos, energía, aguas municipales, telecomunicaciones)')

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ─────────────────────────── Worker / API PrestaShop ───────────────────────────
def _req(method, path, body=None, tries=5):
    """Petición al API PrestaShop vía Worker. Reintenta ante 405 (propagación del
    Worker) y 5xx transitorios."""
    url = f"{WORKER_URL}/ps/{path.lstrip('/')}"
    headers = {'X-PS-Token': PS_TOKEN, 'Content-Type': 'application/xml'}
    for i in range(tries):
        r = requests.request(method, url, headers=headers, data=body, timeout=30)
        if r.status_code < 400:
            return r.status_code, r.text
        if r.status_code in (405, 429, 500, 502, 503, 504):
            time.sleep(1.5 * (i + 1))
            continue
        return r.status_code, r.text
    return r.status_code, r.text


def ps_get(path):
    code, text = _req('GET', path)
    return text if code == 200 else ''


def ps_put(path, xml_body):
    return _req('PUT', path, body=xml_body.encode('utf-8'))


def product_ids(limit):
    xml = ps_get(f"products?limit={limit}")
    return re.findall(r'<product id="(\d+)"', xml)


# ─────────────────────────── Parseo / edición de XML ───────────────────────────
def lang_cdata(xml, tag, lang=DEFAULT_LANG):
    """Valor CDATA de <tag> para un idioma concreto (o el primero que haya)."""
    m = re.search(rf'<{tag}>.*?<language id="{lang}"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>', xml, re.S)
    if not m:
        m = re.search(rf'<{tag}>.*?<!\[CDATA\[(.*?)\]\]>', xml, re.S)
    return html.unescape(m.group(1).strip()) if m else ''


def set_lang_cdata(xml, tag, new_value, lang=DEFAULT_LANG):
    """Sustituye SOLO el CDATA de <tag> para el idioma dado, conservando el resto."""
    pat = re.compile(rf'(<{tag}>.*?<language id="{lang}"[^>]*>)\s*<!\[CDATA\[.*?\]\]>(\s*</language>)', re.S)
    repl = lambda m: m.group(1) + f'<![CDATA[{new_value}]]>' + m.group(2)
    new_xml, n = pat.subn(repl, xml)
    return new_xml, n


# Campos de solo-lectura que PrestaShop rechaza en un PUT (hay que quitarlos del XML de GET).
# Ojo: algunos nodos llevan atributos (p. ej. <manufacturer_name notFilterable="true">).
READONLY_NODES = ['manufacturer_name', 'quantity']


def strip_readonly(xml):
    for tag in READONLY_NODES:
        xml = re.sub(rf'\s*<{tag}(\s[^>]*)?>.*?</{tag}>', '', xml, flags=re.S)
        xml = re.sub(rf'\s*<{tag}(\s[^>]*)?/>', '', xml)
    return xml


# ─────────────────────────── Generación de meta ───────────────────────────
def needs_meta(mt, md):
    return (not mt or len(mt) < 15 or len(mt) > 70 or
            not md or len(md) < 70 or len(md) > 160)


def gen_meta(model, name, mt, md):
    prompt = (
        f'Producto de {BRAND} ({SECTOR}).\n'
        f'Nombre del producto: "{name}".\n'
        f'Meta title actual: "{mt}".\nMeta description actual: "{md}".\n'
        'Optimiza el SEO para las búsquedas reales del sector. Responde SOLO JSON:\n'
        '{"meta_title":"<=60 caracteres, incluye el tipo de producto y la marca al final si cabe",'
        '"meta_description":"120-155 caracteres, con un beneficio concreto y un término de búsqueda del sector, tono profesional"}'
    )
    raw = generate_with_retry(
        model, prompt,
        generation_config={'temperature': 0.4, 'max_output_tokens': 400,
                           'response_mime_type': 'application/json'},
        stream=False,
    )
    data = json.loads(raw)
    return (data.get('meta_title', '').strip(), data.get('meta_description', '').strip())


# ─────────────────────────── Main ───────────────────────────
def run(ids=None, limit=PS_MAX, apply=False):
    if not PS_TOKEN:
        print('ERROR: falta PS_PROXY_TOKEN (token del Worker).'); sys.exit(1)
    if not GEMINI_API_KEY:
        print('ERROR: falta GEMINI_API_KEY.'); sys.exit(1)

    if ids is None:
        ids = product_ids(limit)
    print(f"{'='*74}\n  PrestaShop SEO · {BRAND} · {len(ids)} productos"
          f"{'  [APLICAR]' if apply else '  [DRY-RUN]'}\n{'='*74}")

    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=(f'Eres consultor SEO técnico senior de {BRAND}: {SECTOR}. '
                            'Escribe en español, tono profesional B2B. Devuelve SOLO JSON.'))
    stats = {'revisados': 0, 'generados': 0, 'aplicados': 0, 'saltados_ok': 0, 'errores': 0}

    for pid in ids:
        xml = ps_get(f"products/{pid}?display=full")
        name = lang_cdata(xml, 'name')
        if not name:
            print(f"  #{pid}  ⚠ sin datos (¿WAF? reintentar)"); stats['errores'] += 1; continue
        mt = lang_cdata(xml, 'meta_title')
        md = lang_cdata(xml, 'meta_description')
        stats['revisados'] += 1
        if not needs_meta(mt, md):
            stats['saltados_ok'] += 1
            print(f"  #{pid}  ✓ meta ya correcta · {name[:45]}")
            continue
        try:
            nt, nd = gen_meta(model, name, mt, md)
        except ResourceExhausted:
            print("\n⏸  Cuota diaria de Gemini agotada. Lo hecho queda guardado. (Salida OK.)")
            break
        except Exception as e:
            print(f"  #{pid}  ✗ Gemini: {type(e).__name__}"); stats['errores'] += 1; continue
        stats['generados'] += 1
        print(f"\n  #{pid} · {name}")
        print(f"     TITLE {len(mt)}→{len(nt)} | {mt or '(vacío)'}\n                → {nt}")
        print(f"     DESC  {len(md)}→{len(nd)} | {(md or '(vacío)')[:80]}\n                → {nd}")

        if apply:
            new_xml = strip_readonly(xml)
            new_xml, n1 = set_lang_cdata(new_xml, 'meta_title', nt)
            new_xml, n2 = set_lang_cdata(new_xml, 'meta_description', nd)
            if n1 == 0 or n2 == 0:
                print("     ✗ no se pudo localizar el nodo de meta para reemplazar"); stats['errores'] += 1; continue
            code, resp = ps_put(f"products/{pid}", new_xml)
            if code in (200, 201):
                stats['aplicados'] += 1
                print("     ✅ aplicado en PrestaShop")
            else:
                stats['errores'] += 1
                print(f"     ✗ PUT HTTP {code}: {resp[:160]}")
        time.sleep(PACING)

    print(f"\n  Resumen: revisados {stats['revisados']} · con meta OK {stats['saltados_ok']} · "
          f"generados {stats['generados']} · aplicados {stats['aplicados']} · errores {stats['errores']}")
    return stats


def main():
    args = sys.argv[1:]
    apply = '--apply' in args
    limit = PS_MAX
    ids = None
    if '--limit' in args:
        try: limit = int(args[args.index('--limit') + 1])
        except Exception: pass
    if '--ids' in args:
        try: ids = [x.strip() for x in args[args.index('--ids') + 1].split(',') if x.strip()]
        except Exception: pass
    run(ids=ids, limit=limit, apply=apply)


if __name__ == '__main__':
    main()