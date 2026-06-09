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

    def _call(self, action, sql, params=None):
        response = requests.post(
            self.url,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'SEO-Agents/1.0 (compatible; PHP-API-Client)',
                'X-Api-Token': self.token
            },
            json={
                'action': action,
                'sql': sql,
                'params': params or []
            },
            timeout=30
        )
        if response.status_code != 200:
            raise RuntimeError(f"API error {response.status_code}: {response.text}")
        return response.json()

    def query(self, sql, params=None):
        """SELECT que devuelve lista de dicts."""
        return self._call('query', sql, params)['rows']

    def query_one(self, sql, params=None):
        """SELECT que devuelve el primer dict o None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        """INSERT/UPDATE/DELETE. Devuelve {'affected': N, 'last_id': X}."""
        return self._call('execute', sql, params)