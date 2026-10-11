# NEXO Stage 15B.3B-2B.1: HTTP Policy Inventory

- Tipo: auditoría read-only y prototipo aislado; no es enforcement
- Base auditada: `08f18cdea0928aefa89449c8b2e69e539451f0f4`
- Blueprint 2A: commit local `851967577707f2f198dd00a870d99a3b45daf571` (`docs: finalize stage 15b3b2a auth integration blueprint`)
- Runtime HTTP: sin cambios; no se importa auth ni se instala middleware
- NEXO_MODULES del inventario: `attachments,decision-runtime` para incluir una ruta opcional
- Resultado de seguridad: **NO-GO** para tratar este inventario como protección o habilitar tráfico

## Alcance y método

El inspector `prototypes/http_policy_inventory.py` recorre el orden real de `app.routes`, resume métodos por `APIRoute`, identifica `WebSocketRoute`, `Mount` y rutas anidadas cuando el ASGI app expone `.routes`, clasifica handlers de framework y serializa un snapshot JSON determinista con SHA-256. Strict mode falla por método sin política, declaración para método no registrado, ruta duplicada/potencialmente sombreada o ambigüedad de matching. `assert_snapshot_current()` detecta cambios posteriores comparando fingerprint.

La app se importó en un proceso de auditoría con `NEXO_DATA_DIR` apuntando a un directorio temporal. No se ejecutó startup para el conteo de rutas y no se accedió a ningún servicio externo. `decision-runtime` registra `/api/decisions` durante su `register()` y quedó incluido. `web-search-searxng` no define rutas HTTP en su módulo; sólo registra una tool. El mount de `StaticFiles` se reporta como frontera `/assets` (no se enumeran todos los archivos/rutas internos del servidor estático).

El snapshot observado tiene **72 entradas**: 71 rutas HTTP y 1 mount estático; 75 bindings HTTP método/ruta (incluye GET y HEAD en cuatro rutas generadas por FastAPI), más 1 binding sintético `MOUNT`. No hay WebSockets ni mounts anidados en el runtime actual. El origen es 65 handlers en `app.main`, 2 rutas de módulos (`attachments`, `decision_runtime`), 4 endpoints FastAPI y el mount de assets. Fingerprint determinista en el entorno de auditoría: `9004f3b2ba682f6b3d5e1b1e4ee8adc1417409f5775a6dac717cc7062b49005d`.

El prototipo no está conectado a `app.main`, a startup/lifespan ni a readiness. `assert_snapshot_current()` detecta drift cuando se invoca, marca el snapshot anterior como `stale` (`valid=False`) y lo rechaza. No bloquea por sí mismo la app real. La arquitectura objetivo es congelar el inventario al final del startup/lifespan, después de todos los registros, antes de disponibilidad; cualquier registro posterior debe invalidar readiness/fallar cerrado. Ese comportamiento aún no está implementado.

## Estado aplicado

El runtime no contiene políticas de acceso ni auth middleware: los **75 bindings HTTP están anónimos**; el mount tampoco tiene policy metadata. El inspector reporta exactamente 76 findings `missing_policy` (75 métodos HTTP + el pseudo-método `MOUNT`). Son omisiones de declaración/inventario, contadas por método y frontera de mount; **no equivalen a 76 vulnerabilidades independientes**. La vulnerabilidad actual es sistémica: la superficie HTTP no tiene enforcement de auth y cada endpoint requiere evaluación de impacto propia.

Hay un warning de matching: el fallback tardío `GET /{path:path}` se solapa con rutas GET anteriores. El orden actual conserva la coincidencia de las rutas anteriores, pero el fallback devuelve el shell HTML para paths desconocidos, incluidos paths `/api/...`; la prueba aislada confirmó `GET /api/not-a-real-route` -> 200 `text/html`. No se detectó ruta anterior que sombree una ruta posterior ni un duplicado exacto.

## Matriz del inventario

La política de la tabla es **objetivo propuesto**. El estado aplicado actual para cada fila es `sin política / anónimo`. Una política futura no elimina las comprobaciones adicionales de ownership/ACL ni las validaciones previas a efectos laterales.

| Método y ruta | Registro | Política objetivo | Riesgo / condición |
|---|---|---|---|
| `GET, HEAD /openapi.json` | FastAPI framework | `admin(diagnostics:read)` o deshabilitar OpenAPI en producción | Actualmente expone schema sin auth |
| `GET, HEAD /docs` | FastAPI framework | `admin(diagnostics:read)` o deshabilitar docs en producción | UI revela superficie de endpoints |
| `GET, HEAD /docs/oauth2-redirect` | FastAPI framework | `admin(diagnostics:read)` o deshabilitar docs | Ruta auxiliar generada; no ignorarla |
| `GET, HEAD /redoc` | FastAPI framework | `admin(diagnostics:read)` o deshabilitar ReDoc | UI revela superficie de endpoints |
| `POST /api/files` | Módulo `attachments` | `user(chat:write)` | Autorización antes de parsear/leer multipart; mantener límite de upload |
| `POST /api/decisions` | Opcional `decision-runtime` | `admin(diagnostics:run)` en el MVP | Endpoint opcional invoca proveedor; manifest/capability no autentica caller |
| `POST /api/settings/retrieval-benchmark` | Core | `admin(diagnostics:run)` | Consume CPU y lee notebook; autorizar antes del job |
| `GET /api/settings/retrieval-benchmark/{job_id}` | Core | `admin(diagnostics:read)` | Resultados/estado de benchmark |
| `GET /api/settings/tools` | Core | `admin(config:read)` | Incluye configuración y presencia de credencial AEMET |
| `PUT /api/settings/tools` | Core | `admin(config:write)` | Cambia configuración/credencial; CSRF |
| `GET /api/mcp/servers` | Core | `admin(mcp:read)` | Metadata de endpoints y herramientas MCP |
| `POST /api/mcp/servers` | Core | `admin(mcp:write)` | Persiste URL/token; validación egress/SSRF sigue siendo necesaria |
| `PUT /api/mcp/servers/{server_id}` | Core | `admin(mcp:write)` | Cambia endpoint/credencial; proteger secreto y destino |
| `PATCH /api/mcp/servers/{server_id}` | Core | `admin(mcp:write)` | Habilita/deshabilita servidor |
| `POST /api/mcp/servers/{server_id}/connect` | Core | `admin(mcp:connect)` | Contacto saliente a destino administrado |
| `PATCH /api/mcp/servers/{server_id}/tools` | Core | `admin(tools:write)` | Cambia exposición de todas las tools del servidor |
| `PATCH /api/mcp/servers/{server_id}/tools/{tool_id:path}` | Core | `admin(tools:write)` | Cambia enabled/action; path converter requiere mantener el orden de matching actual |
| `DELETE /api/mcp/servers/{server_id}` | Core | `admin(mcp:write)` | Borra configuración y credenciales asociadas |
| `POST /api/interactions/{interaction_id}` | Core | `user(agents:interact)` | Owner por run/conversación, expiración y transición one-shot; UUID no autentica |
| `GET /api/preferences` | Core | `user(config:read)` | Hoy global; primera migración debe asignar valor existente al admin |
| `PATCH /api/preferences` | Core | `user(config:write)` | Hoy global; particionar por usuario y CSRF |
| `GET /api/health` | Core | `public` sólo tras reducir a liveness `{status}` | Hoy incluye versión y salud de vector store/detalle potencial |
| `GET /api/settings/embeddings` | Core | `admin(config:read)` | Devuelve configuración RAG, providers/modelos y health; no revelar para anónimo |
| `PUT /api/settings/embeddings` | Core | `admin(config:write)` | Cambia proveedor/config e inicia persistencia; validar referencias antes de escribir |
| `POST /api/settings/embeddings/test` | Core | `admin(providers:test)` | Llamada saliente a proveedor |
| `POST /api/settings/embeddings/test-reranker` | Core | `admin(providers:test)` | Llamada saliente y envío de consulta |
| `PATCH /api/providers/{pid}/models/{model_id:path}` | Core | `admin(providers:write)` | Mutación de capacidades del modelo |
| `GET /api/modules` | Core | `admin(config:read)` | Expone módulos/capacidades/estado de dependencias |
| `GET /api/tools` | Core | `admin(tools:read)` | Catálogo interno de tools |
| `GET /api/agents` | Core | `admin(agents:read)` | AgentProfiles globales |
| `POST /api/agents` | Core | `admin(agents:write)` | Instrucciones, provider, notebook y tools allowlist |
| `GET /api/agents/{profile_id}` | Core | `admin(agents:read)` | Lee configuración/instrucciones |
| `PATCH /api/agents/{profile_id}` | Core | `admin(agents:write)` | Cambia configuración/tool allowlist |
| `DELETE /api/agents/{profile_id}` | Core | `admin(agents:write)` | Borrado de perfil referenciado |
| `GET /api/notebooks` | Core | `admin(rag:read)` | Filtrar por ownership si se habilita más de un usuario |
| `POST /api/notebooks` | Core | `admin(rag:write)` | En MVP crea recurso administrado; ownership futura explícita |
| `GET /api/notebooks/{notebook_id}` | Core | `admin(rag:read)` | Comprobar ownership/ACL en misma consulta |
| `PATCH /api/notebooks/{notebook_id}` | Core | `admin(rag:write)` | Ownership antes de update; CSRF |
| `DELETE /api/notebooks/{notebook_id}` | Core | `admin(rag:write)` | Cascada de datos; confirmación/re-auth opcional para destrucción |
| `GET /api/notebooks/{notebook_id}/sources` | Core | `admin(rag:read)` | Hereda acceso del notebook |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}` | Core | `admin(rag:read)` | Hereda notebook y filtra source bajo el mismo padre |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/document` | Core | `admin(rag:read_content)` | Entrega documento canónico completo |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/index` | Core | `admin(rag:read)` | Metadata/vector counts; ownership del notebook |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/integrity` | Core | `admin(rag:diagnostics)` | Diagnósticos sobre índice y datos del notebook |
| `POST /api/notebooks/{notebook_id}/sources/{source_id}/ingest` | Core | `admin(rag:write)` | Cómputo/persistencia antes del cual validar owner |
| `POST /api/notebooks/{notebook_id}/sources/{source_id}/index` | Core | `admin(rag:write)` | Embeddings y egress al provider |
| `POST /api/notebooks/{notebook_id}/retrieve` | Core | `admin(rag:query)` | Recupera texto; comprobar owner antes de embedding/provider |
| `POST /api/notebooks/{notebook_id}/retrieve/diagnostics` | Core | `admin(rag:diagnostics)` | Expone candidatos/ranks y matches de contenido |
| `POST /api/notebooks/{notebook_id}/sources` | Core | `admin(rag:write)` | JSON URL puede causar SSRF; multipart se autoriza antes de leer body |
| `DELETE /api/notebooks/{notebook_id}/sources/{source_id}` | Core | `admin(rag:write)` | Borra fuente/documento/índice derivado |
| `GET /api/lab/shadow` | Core | `admin(diagnostics:read)` | Respuestas/metadata derivada por conversación; revisar owner filter |
| `GET /api/lab/diagnostics` | Core | `admin(diagnostics:read)` | Diagnósticos globales; redacción sigue necesaria |
| `GET /api/lab/traces` | Core | `admin(diagnostics:read)` | Runs/traces pueden referir contenido y owners |
| `GET /api/lab/runs` | Core | `admin(diagnostics:read)` | Metadata de runs; filtrar por owner cuando corresponda |
| `GET /api/lab/runs/{run_id}` | Core | `admin(diagnostics:read)` | Owner vía conversación/run padre |
| `GET /api/lab/runs/{run_id}/events` | Core | `admin(diagnostics:read)` | Owner vía run padre |
| `GET /api/lab/traces/{message_id}` | Core | `admin(diagnostics:read)` | Owner vía mensaje/conversación |
| `GET /api/providers` | Core | `admin(providers:read)` | Endpoint omite key en claro, pero revela metadata sensible |
| `POST /api/providers` | Core | `admin(providers:write)` | API key y endpoint; SSRF/egress aparte; CSRF |
| `PUT /api/providers/{pid}` | Core | `admin(providers:write)` | Actualiza endpoint/modelos/key |
| `DELETE /api/providers/{pid}` | Core | `admin(providers:write)` | Borrado/cascada; re-auth operacional a decidir |
| `POST /api/providers/{pid}/refresh` | Core | `admin(providers:test)` | Contacto a endpoint configurado |
| `DELETE /api/providers/{pid}/models/{model_id:path}` | Core | `admin(providers:write)` | Borra modelo de provider |
| `GET /api/conversations` | Core | `user(chat:read)` | Query actual global; filtrar `owner_user_id` en SQL |
| `GET /api/conversations/{cid}` | Core | `user(chat:read)` | Mensajes, citas, documento excerpt, approvals y metadata; owner antes de joins |
| `POST /api/approvals/{approval_id}` | Core | `user(tools:approve)` | Owner, sesión actual, CSRF, expiry, snapshot/fingerprint, one-shot antes de tool |
| `DELETE /api/conversations/{cid}` | Core | `user(chat:delete)` | Owner en DELETE; cascadas y CSRF |
| `PATCH /api/conversations/{cid}` | Core | `user(chat:write)` | Owner de conversación y validación de profile/notebook |
| `POST /api/conversations/{cid}/branch/{message_id}` | Core | `user(chat:write)` | Owner del origen; rama mantiene caller owner |
| `POST /api/chat` | Core | `user(chat:write)` | Además `rag:query` si se usa RAG; owner, CSRF y autorización antes de mensaje/run/hooks/provider/stream |
| `MOUNT /assets` | Mount `StaticFiles` | `public` explícita en boundary del mount | No se inspecciona cada asset; garantizar que no hay datos privados en el directorio |
| `GET /{path:path}` | Catch-all frontend en `app.main` | **Pendiente de cambio de diseño**: acotar a shell frontend; nunca devolver HTML para `/api/*` desconocido | Recibe unknown API y hoy responde index HTML 200; broad fallback warning, ordenado después de rutas conocidas |

No se observó una ruta de login, sesión o bootstrap en el runtime actual. `/api/auth/*` de `prototypes.admin_auth_http` es sólo fixture aislado y no forma parte de este inventario.

La decisión de MVP para `/api/decisions` es exclusivamente administrativa: `admin(diagnostics:run)`. En una fase futura, servicio sólo podrá usarse si hay identidad de workload autenticada y un scope específico/audience para decision; un `Principal(kind="service")` lógico o la capability de módulo no bastan. No se habilita service access en este checkpoint.

### Contrato SPA para rutas desconocidas

El fallback objetivo es: rutas UI conocidas pueden servir el shell estático; cualquier ruta `/api/*` sin handler registrado responde **404 y nunca HTML 200**. La futura implementación debe resolver esta separación en routing antes de habilitar auth, sin usar un permiso público por prefijo y sin interceptar rutas API existentes. Este contrato se documenta ahora; no se cambió el fallback productivo en 2B.1.

## Criterios y ambigüedades

- **Públicas objetivo:** sólo health reducido, mount static assets y shell frontend acotado. Ninguna de ellas está protegida o limitada de esa forma hoy; el shell público existe como catch-all demasiado amplio.
- **Privadas aún anónimas:** todos los endpoints API inventariados, incluidos `/api/health` en su forma actual y `/api/decisions` cuando se habilita el módulo. Todas las 75 combinaciones HTTP carecen de declaración/guard.
- **Decisión fijada:** `POST /api/decisions` usa `admin(diagnostics:run)` para MVP. Service requiere identity/scope/audience autenticados en una fase futura.
- **Pendiente de routing:** `GET /{path:path}` debe separarse/acotarse para que `/api/*` desconocido sea 404. Las rutas framework tienen objetivo explícito admin-only o deshabilitado, no público por default.
- **Matching:** una advertencia para el catch-all GET al final; no se considera que sombree rutas anteriores, pero sí captura desconocidas. El path converter MCP `/tools/{tool_id:path}` fue comprobado sin falso overlap con `/tools` al respetar el separador slash.
- **Límite de matching del prototipo:** solapamiento de parámetros se calcula conservadoramente por segmentos y puede informar candidatos que requieren confirmar contra los converters/ruta Starlette real. No sustituye pruebas ASGI de matching.
- **Módulos:** `attachments` es el default y añade una ruta. `decision-runtime` es opcional y añade `/api/decisions` por `context.app.post()`. `web-search-searxng` no registra rutas HTTP; sus tools no son endpoints. Módulos futuros pueden registrar rutas luego del snapshot; hoy el inspector no está conectado para detenerlo.

## Pruebas y verificación

Pruebas focales: `.venv/bin/python -m unittest tests.test_stage15b3b2b1_http_policy_inventory -v` -> **17 tests, OK**. Cubren policies public/user/admin/service, no policy + strict, omisiones contadas por método/mount, declaración duplicada, wildcard y policy mal formada, métodos HTTP de extensión, GET/POST distintos, routes framework, mount estático, subapp montada, módulo opcional, WebSocket, duplicados/overlaps, catch-all ordering, path converter, drift post-snapshot (incluida invalidación `stale`) y determinismo.

Verificación HTTP aislada: TestClient con `NEXO_DATA_DIR` temporal y sólo módulo attachments -> `GET /api/health` 200, shell `/` 200 HTML, `/api/not-a-real-route` 200 HTML. La migración/startup ocurrió sólo en DB temporal. Esto confirma el comportamiento actual y también documenta el riesgo del fallback; no es una prueba de autenticación ni cambia endpoints.

No se editó `app/main.py`, AgentRuntime, módulos productivos, RAG, providers, MCP, SSE, frontend, migraciones ni Docker. `prototypes/http_policy_inventory.py` no se importa desde el runtime. Software Docs Shared no se modificó y no se creó propuesta porque no surgió una decisión arquitectónica nueva que justifique otro artefacto.

## Riesgos y decisión

El informe clasifica el código del HEAD, no una instalación productiva. Aun cuando las 15 pruebas focales y el quality gate pasen, esto no valida el freeze/lifespan en despliegue, middleware, autorización real, ownership, body-read ordering, SSE, CSRF, scopes, los permission combinations ni las rutas del proxy.

**NO-GO para consolidar/desplegar el enforcement 2B.1 como seguridad efectiva.** GO para revisar el prototipo y usarlo como fundamento del siguiente checkpoint. Requerir autorización posterior para conectar el inventario al lifecycle/registry real, decidir las dos políticas pendientes, cerrar el catch-all `/api/*` y cubrir todos los módulos de configuración antes de activar auth. No iniciar 2B.2 ni 15C.

## Estado de quality gates

- `./scripts/nexo check` -> **307 passed, 1 skipped**, exit 0. 5 warnings Python: Starlette `BlockingPortal` y FastAPI `on_event` deprecados; 3 avisos Node `MODULE_TYPELESS_PACKAGE_JSON` para assets JS. Sin fallos.
- `git diff --check` -> OK, sin salida. Los tres archivos nuevos también pasaron `git diff --no-index --check /dev/null <archivo>`.
- `git status --short --branch` -> `## main...origin/main [ahead 9]`; sólo estos tres untracked: este informe, `prototypes/http_policy_inventory.py` y `tests/test_stage15b3b2b1_http_policy_inventory.py`.
- Commit 2A: `851967577707f2f198dd00a870d99a3b45daf571`; cambios 2B.1 permanecen sin commit.
