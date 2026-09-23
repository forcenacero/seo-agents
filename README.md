# SEO Agents

Sistema autónomo de SEO multi-cliente. Lee Search Console cada día, guarda métricas históricas en una base de datos compartida y las muestra en un dashboard visual. Diseñado para crecer hasta seis agentes especializados.

## Estado actual

| Componente | Estado |
|---|---|
| Base de datos MySQL en Raiola | ✅ Funcionando |
| Endpoint PHP intermediario | ✅ Funcionando |
| Cliente Python para BD | ✅ Funcionando |
| Agente Performance | ✅ Funcionando, ejecución diaria |
| Dashboard web | ✅ Funcionando |
| Agente Auditor | ⏳ Pendiente |
| Agente Espía | ⏳ Pendiente |
| Agente Estratega | ⏳ Pendiente |
| Agente Optimizador | ⏳ Pendiente |
| Agente Redactor | ⏳ Pendiente |

## Arquitectura

```
┌─────────────────┐
│ GitHub Actions  │  ← Ejecuta agentes cada día
└────────┬────────┘
         │ HTTPS + token
         ▼
┌─────────────────┐         ┌──────────────────┐
│   Endpoint PHP  │ ◄────── │ Dashboard HTML   │
│   (Raiola)      │         │ (Raiola)         │
└────────┬────────┘         └──────────────────┘
         │ MySQL local
         ▼
┌─────────────────┐
│  MySQL          │  ← Memoria compartida
│  (Raiola)       │
└─────────────────┘
         ▲
         │ Lee
┌─────────────────┐
│ Search Console  │
│ DataForSEO      │
│ WordPress API   │
│ Claude API      │
└─────────────────┘
```

Decisión clave: la base de datos vive en hosting compartido (Raiola) y el firewall bloquea conexiones MySQL desde fuera. Por eso usamos un endpoint PHP que actúa de intermediario: los scripts Python le hablan por HTTPS y el PHP, desde dentro del servidor, accede a MySQL en localhost.

## Ubicación de cada cosa

| Qué | Dónde |
|---|---|
| Código de los agentes | Repo GitHub `forcenacero/seo-agents` |
| Base de datos | Raiola, BD `upcoflyc_seo_agents` |
| Endpoint API | `https://upcofly.com/seo-api/` *(ajustar si está en otra ruta)* |
| Dashboard | `https://upcofly.com/dashboard/` *(ajustar si está en otra ruta)* |
| Service Account de Google | Archivo local `credentials/gsc-service-account.json` |
| Credenciales en producción | GitHub Secrets del repo |

## Estructura del repo

```
seo-agents/
├── .github/workflows/
│   └── performance.yml          # Cron diario del agente Performance
├── agents/
│   ├── db_client.py             # Cliente HTTP que habla con el endpoint
│   ├── performance.py           # Agente Performance (lee GSC)
│   └── import_clients.py        # Importador masivo desde CSV
├── credentials/                 # JSON de Google (NO sube a git)
│   └── gsc-service-account.json
├── data/                        # CSVs con credenciales (NO sube a git)
├── venv/                        # Entorno virtual Python (NO sube a git)
├── .env                         # Credenciales locales (NO sube a git)
├── .gitignore
├── requirements.txt
└── README.md
```

## Esquema de la base de datos

| Tabla | Propósito |
|---|---|
| `clients` | Ficha de cada cliente: dominio, tono de voz, criterios SEO, credenciales WP, propiedad GSC |
| `pages` | Páginas conocidas del cliente con estado técnico y métricas agregadas |
| `keywords` | Keywords trackeadas con volumen, dificultad, intención |
| `tasks` | Cola de acciones priorizadas generadas por el Estratega |
| `content_drafts` | Borradores pendientes de aprobación humana |
| `metrics_daily` | Totales diarios por cliente (clicks, impresiones, posición media) |
| `metrics_pages_daily` | Métricas diarias por URL individual |
| `metrics_keywords_daily` | Métricas diarias por keyword individual |
| `agent_runs` | Log de cada ejecución de cualquier agente (estado, tokens, coste, errores) |

Todas usan `utf8mb4` para soportar caracteres internacionales (CJK, emojis, etc.).

## Cómo añadir un cliente nuevo

1. **En Search Console**: ir a Configuración → Usuarios → añadir el email del Service Account (`client_email` del JSON) con permiso "Completo".
2. **En WordPress del cliente**: generar una application password (Usuarios → Tu perfil → Application Passwords).
3. **En el CSV** `data/clients.csv`: añadir una fila con dominio, nombre, tono de voz, criterios, credenciales WP y propiedad GSC.
4. **Importar**:
   ```bash
   python agents/import_clients.py data/clients.csv
   ```
5. **Verificar** en DBeaver:
   ```sql
   SELECT id, domain, name, active FROM clients;
   ```

El script es idempotente: si el cliente ya existe, lo actualiza.

## Cómo ejecutar los agentes localmente

```bash
source venv/bin/activate
python agents/performance.py
```

El script lee `.env` con credenciales de la BD y el endpoint, y el JSON de Google Service Account desde `credentials/`.

## Cómo se ejecutan en producción

GitHub Actions corre cada workflow según su cron. Performance se ejecuta cada día a las **5:00 UTC** (7:00 hora española en verano, 6:00 en invierno).

También puede dispararse manualmente: GitHub → repo → Actions → Performance Agent → Run workflow.

## Secrets de GitHub

| Nombre | Contenido |
|---|---|
| `DB_API_URL` | URL del endpoint, ej. `https://upcofly.com/seo-api/` |
| `DB_API_TOKEN` | Token de 64 caracteres del endpoint (idéntico al de `config.php`) |
| `GSC_SERVICE_ACCOUNT_JSON` | Contenido completo del JSON de Google |

Si se rota cualquiera de estos, actualizar también el archivo correspondiente en local o en el servidor.

## Endpoint PHP

Vive en el hosting de Raiola en `public_html/seo-api/`. Estructura:

```
seo-api/
├── .htaccess          # Fuerza HTTPS, bloquea acceso a config.php
├── config.php         # Credenciales BD + token (NO se sube a git)
├── index.php          # Router de acciones
└── handlers/
    ├── query.php       # SELECT con parámetros
    ├── execute.php     # INSERT/UPDATE/DELETE individuales y en lote
    └── dashboard.php   # Acciones de alto nivel para el dashboard
```

Acciones disponibles:

| Acción | Uso |
|---|---|
| `query` | SELECT con parámetros, devuelve filas |
| `execute` | INSERT/UPDATE/DELETE individual o en lote (`params_batch`) |
| `dashboard` | Acciones agregadas para el dashboard (ver lista abajo) |

Acciones del dashboard (`dashboard_action`):

- `list_clients` - lista clientes activos
- `summary` - métricas resumen 7d vs 7d anteriores
- `daily_traffic` - tráfico diario últimos 30 días
- `top_pages` - 20 URLs con más clicks
- `top_keywords` - 30 keywords con más clicks
- `opportunities` - keywords en posición 5-15 con buen volumen
- `trends` - páginas que más cambian entre periodos
- `agents_status` - estado de cada agente
- `recent_runs` - últimas 20 ejecuciones

## Dashboard

URL: `https://upcofly.com/dashboard/`

Acceso: API Token (el mismo que usa el endpoint). Se guarda en `sessionStorage` del navegador.

Stack: HTML + JavaScript vanilla + Chart.js. Cero build, cero framework. Se sube por FTP y funciona.

## Cómo añadir un agente nuevo (patrón)

Cada agente sigue este patrón:

1. **Script en `agents/nombre.py`** que:
   - Carga `.env` con `load_dotenv()`
   - Instancia `db = DBClient()`
   - Recorre los clientes activos: `db.query("SELECT ... FROM clients WHERE active = 1")`
   - Para cada cliente: lee fuentes externas, escribe en las tablas correspondientes, registra ejecución en `agent_runs`

2. **Workflow en `.github/workflows/nombre.yml`** con su cron y los secrets necesarios.

3. **Acción en el dashboard** (opcional): añadir un `case` en `dashboard.php` y una sección en `index.html`. Las filas de "Estado de los agentes" en el dashboard se rellenan solas si el agente escribe en `agent_runs`.

Nombres convencionales para `agent_name` en `agent_runs`:
- `performance` ✅ (ya activo)
- `auditor`
- `spy`
- `strategist`
- `optimizer`
- `writer`

## Mantenimiento

**Diario**: nada. El sistema corre solo.

**Semanal**: mirar el dashboard. Verificar que en "Últimas ejecuciones" no hay ejecuciones rojas recientes. Si las hay, abrir GitHub Actions y ver el log.

**Mensual**: revisar `agent_runs` para ver evolución de coste y tiempos. Limpiar logs antiguos si hace falta:

```sql
DELETE FROM agent_runs WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);
```

**Trimestral**: rotar el `API_TOKEN`. Generar nuevo con `openssl rand -hex 32`, actualizar en `config.php` del hosting y en el secret `DB_API_TOKEN` de GitHub.

## Resolución de problemas

**El workflow falla en GitHub Actions con error 403 de Search Console**:
El Service Account no tiene permiso en la propiedad de ese cliente. Añadirlo en Search Console → Configuración → Usuarios.

**El workflow falla con error 500 del endpoint**:
Mirar el log del workflow para el detalle. Causas típicas: error de SQL, tabla que falta, charset incorrecto en una keyword.

**El endpoint da error 403 HTML de Apache (ModSecurity)**:
Raiola está bloqueando la petición. Solución: en cPanel → ModSecurity → desactivar para el dominio del endpoint, o pedir a soporte que añada una excepción.

**El dashboard muestra "Token inválido"**:
Verificar que el token coincide entre `config.php` del endpoint y el que estás pegando en el login.

**Una keyword o URL provoca SQL error con `Incorrect string value`**:
La tabla no está en `utf8mb4`. Ejecutar:
```sql
ALTER TABLE nombre_tabla CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

## Decisiones de arquitectura

**¿Por qué endpoint PHP y no Supabase?**
El hosting compartido de Raiola tiene el puerto 3306 cerrado a internet. Las opciones eran: mover la BD a Supabase (servicio externo), montar túnel SSH (frágil con cron en la nube), o crear un endpoint PHP intermediario. El endpoint mantiene todo "en casa" sin dependencias externas adicionales.

**¿Por qué inserts en lote?**
Una ejecución del Performance puede insertar 1.000+ filas. Una request HTTP por fila es lento y se topa con timeouts. Con `params_batch` se hacen 1 request por cada 50 filas, dentro de una transacción.

**¿Por qué placeholders `?` y no `%s`?**
PDO (PHP) usa `?` como placeholder posicional. Pymysql (Python) usa `%s`. Como las queries se envían al endpoint y se ejecutan en PHP, los placeholders son `?`.

**¿Por qué Service Account y no OAuth?**
Service Account no requiere interacción del usuario para renovar tokens. Perfecto para automatización en GitHub Actions. OAuth tendría que renovarse periódicamente con intervención humana.

## Histórico del proyecto

- **Junio 2026** - Setup inicial: BD en Raiola, endpoint PHP, agente Performance básico, GitHub Actions diario.
- **Junio 2026** - Performance enriquecido: añadidas tablas `metrics_pages_daily` y `metrics_keywords_daily`. Migración a `utf8mb4`. Inserts en lote.
- **Junio 2026** - Dashboard web con 8 secciones (resumen, gráfica, top páginas, top keywords, oportunidades, tendencias, estado de agentes, log).
- **Junio 2026** - Importador masivo de clientes desde CSV.

## Contacto

David Forcen - Upcofly  
[upcofly.com](https://upcofly.com)
