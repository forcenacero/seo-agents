"""
Cliente para hablar con el endpoint PHP en lugar de conectar directo a MySQL.
"""
import os
import gzip
import zlib
import requests

# Brotli es opcional; si está instalado, podemos descomprimir respuestas brotli
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False


class DBClient:
    def __init__(self):
        self.url = os.getenv('DB_API_URL')
        self.token = os.getenv('DB_API_TOKEN')
        if not self.url or not self.token:
            raise RuntimeError("Faltan DB_API_URL o DB_API_TOKEN en el entorno")

    def _post(self, payload, timeout=30):
        response = requests.post(
            self.url,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
                # IDENTITY = pedir sin compresión. Si el servidor lo respeta, llega plano.
                'Accept-Encoding': 'identity',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'X-Api-Token': self.token,
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive'
            },
            json=payload,
            timeout=timeout
        )
        
        # Mensaje de error con HTTP status
        if response.status_code != 200:
            raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
        
        # Obtener el cuerpo: si el servidor ignoró identity y comprimió con brotli,
        # descomprimimos manualmente (requests no lo hace por defecto).
        raw_text = self._decode_body(response)
        
        # Verificar que la respuesta es JSON válido
        try:
            import json
            data = json.loads(raw_text)
        except Exception:
            raise RuntimeError(f"Respuesta no es JSON: {raw_text[:500]}")
        
        # Si el endpoint devuelve un error explícito en el JSON, mostrarlo claro
        if isinstance(data, dict) and 'error' in data:
            detail = data.get('detail', 'sin detalle')
            raise RuntimeError(f"API devolvió error: {data['error']} | Detalle: {detail}")
        
        # Imunify360 a veces devuelve 200 OK pero con un 'message' de bloqueo
        if isinstance(data, dict) and 'message' in data and 'denied' in str(data.get('message', '')).lower():
            raise RuntimeError(f"Bloqueado por firewall: {data['message']}")
        
        return data

    def _decode_body(self, response):
        """Descomprime el cuerpo si llega comprimido a pesar de Accept-Encoding: identity."""
        encoding = response.headers.get('Content-Encoding', '').lower()
        raw_bytes = response.content
        
        if encoding == 'br':
            if not HAS_BROTLI:
                raise RuntimeError(
                    "El servidor envió brotli pero la librería 'brotli' no está instalada. "
                    "Añade 'brotli' a requirements.txt"
                )
            try:
                decoded = brotli.decompress(raw_bytes)
                return decoded.decode('utf-8', errors='replace')
            except Exception as e:
                raise RuntimeError(f"Error descomprimiendo brotli: {e}")
        
        if encoding == 'gzip':
            try:
                decoded = gzip.decompress(raw_bytes)
                return decoded.decode('utf-8', errors='replace')
            except Exception as e:
                raise RuntimeError(f"Error descomprimiendo gzip: {e}")
        
        if encoding == 'deflate':
            try:
                decoded = zlib.decompress(raw_bytes)
                return decoded.decode('utf-8', errors='replace')
            except Exception as e:
                raise RuntimeError(f"Error descomprimiendo deflate: {e}")
        
        # Sin compresión declarada: usar response.text como siempre
        return response.text

    def query(self, sql, params=None):
        """SELECT que devuelve lista de dicts."""
        result = self._post({'action': 'query', 'sql': sql, 'params': params or []})
        if 'rows' not in result:
            raise RuntimeError(f"Respuesta inesperada (sin 'rows'): {str(result)[:300]}")
        return result['rows']

    def query_one(self, sql, params=None):
        """SELECT que devuelve el primer dict o None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        """INSERT/UPDATE/DELETE de una sola fila."""
        return self._post({'action': 'execute', 'sql': sql, 'params': params or []})

    def execute_many(self, sql, params_batch, batch_size=50):
        """INSERT/UPDATE/DELETE en lote. Trocea automáticamente en bloques de batch_size filas."""
        total = 0
        for i in range(0, len(params_batch), batch_size):
            chunk = params_batch[i:i+batch_size]
            result = self._post(
                {'action': 'execute', 'sql': sql, 'params_batch': chunk},
                timeout=60
            )
            total += result.get('affected', 0)
        return total