# NEXO Stage 15B.3B-1: Authentication Core

- Estado: core aislado implementado y probado; detenido para revisión
- Base de implementación: `7e861b2` (decisiones Stage 15B.3B-0 consolidadas)
- Estado al inicio: HEAD `4ad1aaf0c5bfe5dc95d25e99bf01da0684b03bd9`; `main`, cuatro commits ahead
- Estado al cierre: QA focal y quality gate pasaron; commit de consolidación pendiente
- Alcance: núcleo reusable, SQLite dedicada de prototipo y contratos FastAPI no montados
- NO desplegar ni exponer; no hay activación de rutas de NEXO
- Stage 15B.3B-2 y Stage 15C: no iniciadas / bloqueadas

## Resultado

Se implementó `prototypes.admin_auth` como núcleo sin dependencia del servidor: bootstrap local one-shot, hash scrypt versionado, sesión opaque persistida por hash, idle/absolute expiry, revocación, rate limiting transaccional, permisos explícitos, principals tipados, eventos mínimos y fail-closed ante fallos SQLite. `prototypes.admin_auth_http` prepara un router de contrato con cookie/CSRF/origen/host para pruebas aisladas.

**Ningún router se incluye en `app.main`; no hay middleware/dependency aplicada a las rutas actuales. No se modificó código de runtime, `app/migrations.py`, SQLite de NEXO, datos, proveedores, AgentRuntime, aprobación/interacciones, MCP, RAG, frontend o Compose.** La implementación no es desplegable ni protege actualmente ninguna API de NEXO.

## Arquitectura implementada

- `AdminAuthStore`: servicio/persistencia sobre una ruta SQLite explícitamente dedicada. Abre una conexión por operación, `BEGIN IMMEDIATE` para bootstrap/login/autorización/revocación, timeout de lock de 5 s y error público estable `authentication_unavailable` ante SQLite/IO. No llama `app.migrations.migrate()`.
- `bootstrap_local(username, password)`: transacción que exige `auth_state.initialized=0`, crea exactamente un administrador y marca estado inicializado. La restricción singleton y transacción serializan carreras; cualquier segundo bootstrap se rechaza.
- `bootstrap_from_console(store)`: helper local con prompt de usuario y `getpass` repetido, comparación constante de confirmación y sin password en argv/env/log. No es una ruta HTTP ni un comando instalado. La futura herramienta de administración local debe decidir cómo localizar DB/auth store sin permitir path ambiguo.
- Contraseñas: `hashlib.scrypt`, stdlib, N=16384/r=8/p=1/dklen=32, salt aleatoria 16 bytes. Encoding persistido `scrypt$version$N$r$p$salt$hash`; verificación valida versión/parámetros/tamaños antes de KDF y compara en tiempo constante. Política del prototipo: 12 caracteres mínimo, 1024 bytes máximo. Parámetros no están calibrados en hosts de producción.
- Sesión: token `token_urlsafe(32)` (256 bits aleatorios), sólo SHA-256 del token en DB. Defaults: idle 1800 s, absolute 43200 s. Cada acceso válido extiende idle sin pasar absolute; no autorizado, expirado o revocado es 401; fallo de DB es 503. Logout, revocación individual, global y reset local invalidan sesiones inmediatamente. Login limpia rows revocadas/expiradas.
- Rate limit: una ventana para la única cuenta, defaults 5 fallos por 900 s y lock de 300 s; verificación y contador serializados por transacción, persistentes entre reinicios. Respuesta de login genérica 401 para credenciales incorrectas o bloqueo. Tras lock, login válido restablece contador; sin éxito el contador se renueva al superar la ventana. El límite por cuenta compartido puede ser usado para DoS temporal contra el único administrador.
- CSRF: token aleatorio de 256 bits entregado por login, almacenado sólo como SHA-256 asociado a la fila de sesión. `POST /api/auth/csrf` rota el token para recuperar estado después de recargar el cliente; exige cookie válida, HTTPS, Host y Origin exactos. Se evita mutar desde GET. Toda mutación de browser pasa CSRF en `X-CSRF-Token`; ausencia/valor erróneo devuelve 403.
- Autorización: `Principal(kind, subject, permissions, delegation_id)` y `authorize()` deniegan por defecto. El admin recibe un conjunto cerrado y explícito de permisos de configuración, providers, secretos (metadata/write/rotate), agentes, tools/MCP, RAG, chat y diagnostics. No se asigna `system:admin`.
- Servicios: allowlist de scopes nominales (`agent-runtime`, `retrieval`) y `service_principal()` niega scopes fuera de ella. Un usuario no puede pasar autorización que requiera `kind=service`; un servicio no gana permisos humanos. Es una separación de tipo/policy dentro del mismo proceso Python, no una credencial o frontera contra código comprometido. El runtime actual no consume estos principals.
- Auditoría mínima: `auth_security_events` conserva tipo y epoch de bootstrap, login/rechazo/rate-limit, expiración, logout, revocación y reset. Logs de aplicación contienen sólo nombre fijo del evento/categoría; nunca username, password, cookie, CSRF, hash, IP, argumentos o causa cruda SQLite. Test busca valores sintéticos en logs y DB.

## Persistencia y migraciones

El prototipo usa un archivo separado de nombre elegido por quien lo prueba, por ejemplo `auth.sqlite3` dentro de un directorio temporal. Se niega `:memory:`, cualquier ruta cuyo basename resuelto sea `nexo.sqlite3` y el path resuelto de `$NEXO_DATA_DIR/nexo.sqlite3` (default `./data/nexo.sqlite3`). DB se fuerza a modo POSIX `0600`. No abrir, crear ni migrar la DB productiva de NEXO.

Tablas del schema aislado versión 1:

| Tabla | Campos relevantes / propósito |
|---|---|
| `auth_schema_version` | Singleton `version=1` del schema aislado; versión desconocida falla cerrado |
| `auth_state` | Singleton `initialized` para impedir claim/bootstrap repetido |
| `auth_admin` | Singleton username y hash de contraseña versionado; timestamps |
| `auth_sessions` | Hash de token PK, hash CSRF, creación/actividad, expiración idle/absolute, revocación |
| `auth_login_guard` | Fallos, inicio de ventana, `locked_until` persistente |
| `auth_security_events` | Sólo tipo de evento y tiempo; sin credenciales o payload |

Preflight encontró la última migración NEXO en **versión 28** (`_migration_28`), `schema_migrations.version` como PK, `migrate()` secuencial y pruebas con DB temporal/idempotencia. No se creó ni ejecutó una migración NEXO y no se reservó la versión 29. El test crea una tabla `conversations` ficticia en la DB aislada, inicializa el store y verifica que el row permanece intacto; el store sólo añade tablas `auth_*`.

En una integración posterior se debe decidir si las tablas se incorporan a NEXO mediante migración nueva (probablemente siguiente número libre al revalidar, no asumir 29 sin revisar HEAD actual), o se conserva auth DB separada y se define backup/atomicidad. Una DB aislada simplifica la prueba, pero login y datos NEXO no comparten transacción; aún no es una arquitectura de despliegue aprobada. No hay estrategia de downgrade automática: rollback de schema/data requerirá backup probado y auth no podrá degradar a modo abierto.

## Contratos FastAPI no activados

`create_auth_router(auth, allowed_origin)` crea un `APIRouter` de fixture y no se importa desde `app.main`:

| Método/ruta del router de contrato | Contrato |
|---|---|
| `POST /api/auth/login` | Valida HTTPS, Host permitido y Origin exacto obligatorio; cuerpo username/password con SecretStr y límites; 401 genérico; éxito pone `__Host-nexo_session` y devuelve CSRF. Respuestas `no-store`. |
| `GET /api/auth/session` | Exige cookie, `config:read`; entrega identidad y permisos mínimos, sin token/CSRF. No muta CSRF. |
| `POST /api/auth/csrf` | Exige cookie, HTTPS, Host/Origin exactos y rota CSRF; respuesta `no-store`. No es endpoint de bootstrap. |
| `POST /api/auth/logout` | Exige cookie, Origin/Host exactos, `config:read` y CSRF; revoca server-side y borra cookie con los mismos flags. |
| `require_permission(...)` | Dependencia reutilizable de prueba: ausente/expirada/revocada -> 401; permiso/CSRF -> 403; storage indisponible -> 503; validación de host/origen -> 400 antes de acción. |

Cookie de sesión: `__Host-nexo_session`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, sin Domain, max-age absoluto. No se devuelve el token bearer en JSON. El contrato exige `request.url.scheme == https`; no inspecciona ni confía cabeceras `Forwarded`/`X-Forwarded-*`. En un futuro proxy sólo se debe permitir la IP confiable explícita en ASGI/Uvicorn; esa configuración no se probó ni se incluye.

Tests montan el router únicamente en una app FastAPI efímera, con HTTPS de TestClient y SQLite tempfile. Incluyen endpoints fixture de lectura/mutación para probar la dependency. Esto prueba contratos, no constituye login visual ni una modificación de rutas reales.

## Pruebas

`tests/test_stage15b3b1_auth_core.py` cubre:

- bootstrap local, confirmación por consola, one-shot y ausencia de endpoint HTTP de bootstrap;
- contraseña incorrecta, respuesta genérica, rate limit persistente y recuperación tras lockout; cambio de username no evade el contador y ventana inválida se rechaza;
- login/logout, store recreado sobre mismo archivo, idle y absolute expiry;
- CSRF ausente/inválido/válido, renovación y header; permiso no declarado denegado;
- revocación individual/global y revocación de sesiones tras reset local;
- concurrencia de bootstrap y ocho logins independientes sobre SQLite;
- fallo de DB a `AuthUnavailable`, preservación de tabla app no relacionada;
- logs/archivo DB sin password, session token o CSRF en claro;
- separación `user`/`service` y scopes no permitidos;
- cookie Secure/HttpOnly/Lax/Path, HTTP/host/origin incorrectos, status 401/403/204 y sesión revocada.

Las pruebas usan exclusivamente passphrases sintéticas `FAKE_*`; no se importó configuración real de proveedor ni runtime.

## Riesgos y límites

1. **No hay enforcement real:** ninguna ruta existente queda protegida. La instancia actual sigue anónima. No exponerla por considerar este checkpoint terminado.
2. **No hay CLI instalable ni recovery operativo:** existe helper de consola y `reset_password_local`, pero no hay command entrypoint, autorización del host, backup runbook o rotación integrada. No ejecutar sobre la DB NEXO.
3. **Store no forma parte del schema/runtime NEXO:** persistencia y restart se verifican sólo con SQLite temporal dedicado. Compatibilidad transaccional y upgrade real requieren diseño/migración posterior.
4. **Un solo admin:** username y permisos singleton; no hay roles asignables, users múltiples, owner_id ni aislamiento de conversaciones/notebooks.
5. **Identidad de servicio lógica:** `Principal` y allowlist de factory no acreditan proceso. Mismo proceso/root puede construir objetos, leer secretos y saltarse el wrapper si se compromete.
6. **Limitación operativa:** cuenta única/global puede ser temporalmente bloqueada por terceros; SQLite `BEGIN IMMEDIATE` serializa KDF y sesiones. Timeout de lock es 5 s. Multi-host, filesystem de red, réplica o alto throughput no están soportados.
7. **Reloj:** TTL usa epoch del host para persistir a través de restart; cambios grandes de reloj pueden afectar vencimientos. Requiere reloj del sistema razonablemente sincronizado y prueba operacional posterior.
8. **Storage/backups:** chmod protege el archivo en POSIX, no directorio, snapshots, backups, root, memoria ni máquina. No cifra el resto de datos/secretos actuales.
9. **Proxy/TLS:** el router sólo valida esquema ASGI y origin/host esperados; no prueba TLS de despliegue, forwarded trust, Domain, móvil o Portainer real.
10. **Abuso browser/XSS:** HttpOnly reduce robo por JavaScript pero no evita operar desde XSS; CSP/sanitización frontend no están en alcance.
11. **Servicio y tool use:** no hay integración ni delegación hacia AgentRuntime; los scopes de prototipo no habilitan llamadas a providers/tools.

## Condiciones para Stage 15B.3B-2

Antes de empezar, revisión explícita de este core y decisión sobre:

- si auth persiste en el SQLite NEXO por migración versionada o en DB separada; confirmar backup/restore y mantener contenidos actuales;
- path y modo de provisión local de secretos/operación para CLI de bootstrap y password reset;
- defaults de scrypt/coste en hardware objetivo, idle 30 min, absolute 12 h, lockout 5/15 min y respuesta de rate limit;
- permiso final por endpoint y rutas públicas permitidas, incluido manejo de paths añadidos por módulos;
- origen/host HTTPS real, proxy trusted IPs y perfil LAN/VPN/Internet/MFA del operador;
- cómo se representa ownership histórico y operación pendiente en la integración, sin activar ruta parcialmente abierta;
- qué autenticidad real se exige para la identidad de servicio y si separar proceso/UID es un gate antes de tool mutativo.

Stage 15B.3B-2 sólo puede preparar integración si en un mismo checkpoint se protegen todas las rutas privadas o se mantiene ingress cerrado. Requiere tests de enumeración de rutas/módulos, IDOR, CSRF, failure DB, SSE/resume, migration rollback, backups y staging aislado; no habilitar login visual o middleware a medias. **Este informe no inicia 15B.3B-2. Stage 15C sigue bloqueada y no se autoriza despliegue/exposición.**

## Verificación

Resultados ejecutados:

- `.venv/bin/python -m unittest tests.test_stage15b3b1_auth_core -v` -> 20 tests, OK.
- `.venv/bin/python -m unittest tests.test_stage15b3b1_auth_core tests.test_stage15b3a_admin_security -v` -> 24 tests, OK (incluye los 4 tests previos de 15B.3A).
- `./scripts/nexo check` -> 282 passed, 1 skipped; frontend regressions OK. Warnings: FastAPI `on_event`, alias Starlette `BlockingPortal` y Node module type; no fallaron pruebas.
- Benchmark no vinculante, macOS arm64: scrypt `N=2^14,r=8,p=1` median 66 ms por hash (5 muestras de 5 hashes; rango 41-98 ms). No equivale a medición en hosts productivos.
- `git diff --check` -> OK para cambios tracked; como los archivos de implementación/documentación son nuevos y no se stagearon, también se ejecutó `git diff --no-index --check /dev/null <archivo>` para cada uno de los cuatro archivos nuevos; sin diagnósticos de whitespace.
- Estado previo a este registro: sólo los cuatro archivos nuevos de esta etapa, sin modificaciones a archivos existentes, sin staging.
- No se ejecutó `./scripts/nexo smoke`, no se abrió ni migró la base NEXO y no hubo despliegue.

No se hizo commit de esta implementación. El commit independiente de 15B.3B-0 (`7e861b2`) se conserva. El stash histórico permanece sin cambios.
