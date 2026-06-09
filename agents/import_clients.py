"""
Script auxiliar para importar clientes desde un CSV.
Uso: python agents/import_clients.py clients_template.csv
"""
import csv
import json
import sys
import os
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_client import DBClient

load_dotenv()
db = DBClient()


def detect_dialect(csv_path):
    """Detecta separador (coma o punto y coma) leyendo la primera línea."""
    with open(csv_path, encoding='utf-8-sig') as f:
        first_line = f.readline()
    
    if first_line.count(';') > first_line.count(','):
        return ';'
    return ','


def import_csv(csv_path):
    if not os.path.isfile(csv_path):
        print(f"✗ Archivo no encontrado: {csv_path}")
        sys.exit(1)
    
    delimiter = detect_dialect(csv_path)
    print(f"  Separador detectado: '{delimiter}'")
    
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        
        # Debug: mostrar cabeceras leídas
        print(f"  Cabeceras encontradas: {reader.fieldnames}")
        
        required = {'domain', 'name', 'tone_of_voice', 'seo_criteria',
                    'wp_api_url', 'wp_user', 'wp_app_password', 'gsc_property'}
        missing = required - set(reader.fieldnames or [])
        if missing:
            print(f"\n✗ Faltan columnas en el CSV: {missing}")
            print(f"  Asegúrate de que la primera fila tiene estos nombres exactos.")
            sys.exit(1)
        
        inserted = 0
        updated = 0
        errors = 0
        
        for row in reader:
            domain = row['domain'].strip()
            if not domain:
                continue
            
            # Validar JSON
            try:
                json.loads(row['seo_criteria'])
            except json.JSONDecodeError as e:
                print(f"  ⚠ {domain}: JSON inválido en seo_criteria → {e}")
                errors += 1
                continue
            
            # Comprobar si ya existe
            existing = db.query_one(
                "SELECT id FROM clients WHERE domain = ?",
                [domain]
            )
            
            params = [
                row['name'].strip(),
                row['tone_of_voice'].strip(),
                row['seo_criteria'].strip(),
                row['wp_api_url'].strip(),
                row['wp_user'].strip(),
                row['wp_app_password'].strip(),
                row['gsc_property'].strip(),
                int(row.get('active', 1) or 1),
            ]
            
            if existing:
                db.execute("""
                    UPDATE clients SET
                        name = ?, tone_of_voice = ?, seo_criteria = ?,
                        wp_api_url = ?, wp_user = ?, wp_app_password = ?,
                        gsc_property = ?, active = ?
                    WHERE domain = ?
                """, params + [domain])
                print(f"  ↻ Actualizado: {domain}")
                updated += 1
            else:
                db.execute("""
                    INSERT INTO clients
                        (name, tone_of_voice, seo_criteria, wp_api_url,
                         wp_user, wp_app_password, gsc_property, active, domain)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, params + [domain])
                print(f"  ✓ Insertado: {domain}")
                inserted += 1
        
        print(f"\nResumen: {inserted} insertados, {updated} actualizados, {errors} errores")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso: python agents/import_clients.py <ruta_csv>")
        sys.exit(1)
    
    import_csv(sys.argv[1])