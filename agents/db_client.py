"""
Cliente para hablar con el endpoint PHP en lugar de conectar directo a MySQL.
"""
import os
import requests


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
                'Accept': 'application/json',
                'User-Agent': 'SEO-Agents/1.0 (compatible; PHP-API-Client)',
                'X-Api-Token': self.token
            },
            json=payload,
            timeout=timeout
        )
        
        # Mensaje de error con HTTP status
        if response.status_code != 200:
            raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
        
        # Verificar que la respuesta es JSON válido
        try:
            data = response.json()
        except Exception:
            raise RuntimeError(f"Respuesta no es JSON: {response.text[:500]}")
        
        # Si el endpoint devuelve un error explícito en el JSON, mostrarlo claro
        if isinstance(data, dict) and 'error' in data:
            detail = data.get('detail', 'sin detalle')
            raise RuntimeError(f"API devolvió error: {data['error']} | Detalle: {detail}")
        
        return data

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