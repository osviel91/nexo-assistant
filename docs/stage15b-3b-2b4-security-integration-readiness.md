# NEXO Stage 15B.3B-2B.4: Security Integration Readiness Assessment

- Modo: auditoría técnica/documental; sin integración ni cambios productivos
- HEAD verificado: `1fd58055dca1f9014b18d862ced024a0057b22f9`
- Rama/worktree: `main`, limpio al inicio; `main...origin/main [ahead 12]`
- Stash histórico: `stash@{0}` (`b7d2662a55bb2519aadd4291a93400c689afb46c`), intacto
- Resultado: **GO para evaluación documental; NO-GO para integración en runtime, activación o exposición**

## 1. Resumen ejecutivo

La implementación está **no preparada para integración en tráfico abierto**. La app real no usa ninguno de los tres prototipos 2B.2/2B.3, carece de usuario/sesión/ownership y sirve anónimamente toda la API. El Compose versionado publica HTTP `8787`; Docker ejecuta como root (sin `USER`); no hay TLS, proxy confiable, firewall o control de ingress declarado por estos archivos. La instalación Portainer real no se inspeccionó.

Condiciones mínimas antes de abrir tráfico: (1) catálogo de rutas exhaustivo y denegación por defecto incorporados al lifecycle, incluidas rutas de framework/módulos; (2) auth store/migración/backup/restore ensayados con rollback y crash recovery; (3) bootstrap local host-side que gestione la DB principal y asignación histórica explícita; (4) ownership aplicado en SQL y atómico con escrituras/aprobaciones; (5) autorización inicial anterior a toda escritura, tarea, lectura de archivo, llamada externa y apertura SSE, más revalidación antes de cada efecto de streams; (6) proxy/HTTPS/origin y publicación de ingress verificados en la instalación; (7) gates de startup/readiness que mantengan privado el servicio hasta completar todo lo anterior.

El inventario actual son **67 rutas/bindings HTTP de aplicación** con módulos `attachments,decision-runtime`, más 8 bindings HTTP de rutas generadas por FastAPI = **75 bindings HTTP**. Se observa además `MOUNT /assets` y `GET /{path:path}`. Confirmado: `POST /api/decisions` objetivo `admin(diagnostics:run)`; `/api/*` desconocida debe ser 404, nunca shell HTML. Ninguna API actual es pública. Sólo se proponen como públicas (con alcance restringido) assets estáticos, shell UI explícito y health reducido a liveness; no se clasifican como públicas por el mero anonimato actual.

**Bloqueos críticos:** C1 API completa anónima/efectos protegibles; C2 orden de autorización no implementado y stream con efectos previos/post-start; C3 migrador productivo no demostrado atómico; C4 DB productiva/backup/versión desconocidos; C5 bootstrap/admin de la DB principal inexistente. **Altos:** ownership/IDOR y aprobaciones legacy; fallback API devuelve HTML 200; configuración de ingress/TLS e UID real desconocida (Compose expone HTTP y la imagen es root); policy snapshot no está conectado al ciclo de vida. No se debe declarar GO productivo hasta cerrar todos los críticos y los altos de activación.

### Integration-ready frente a activation-ready

**Integration-ready** significa que se puede continuar trabajo de ingeniería en un entorno controlado, con ingress cerrado: CP2–CP6 pueden producir código y pruebas de integración, cada cual sujeto a sus propios gates. No autoriza desplegar ni exponer esa versión, ni siquiera temporalmente en una red accesible. En particular, completar CP4 no convierte la app en segura: el guard HTTP es integración estructural y permanece **no activable** hasta superar CP5 (ownership y autorización SQL dentro de las operaciones) y CP6 (SSE, delegación y autorización de cada efecto protegido). Un runtime con guard pero sin esos controles se considera incompleto y debe permanecer fuera de ingress.

**Activation-ready** requiere todos los gates técnicos y operativos de CP7: CP2–CP6 completos, enforcement de todas las rutas y operaciones, migración/bootstrap/ownership coherentes, SSE y efectos protegidos, backup/restore demostrado, y topología/HTTPS/ingress verificados. Ningún checkpoint individual ni `/api/health` 200 concede activation-ready. Si hay dudas o faltan evidencias, ingress cerrado.

## 2. Evidencia y metodología

Preflight read-only: `AGENTS.md`; HEAD/rama/status; hash y stat del stash sin aplicarlo; informes de 2A, 2B.1, 2B.2, 2B.3 y 1/1R; implementaciones aisladas; `app/main.py`, módulos, `app/migrations.py`, Docker/Compose/Portainer y configuración de ejemplo. Se importó FastAPI con `NEXO_DATA_DIR` temporal y `NEXO_MODULES=attachments,decision-runtime`; no se llamó startup y no se abrió ninguna DB productiva. Ruta contada desde `app.routes` y métodos de `APIRoute`. Los 8 bindings framework son GET/HEAD para `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`. Una ruta `GET /{path:path}` y un mount se cuentan aparte. Resultado confirma 75 métodos HTTP, pero no equivale a 75 vulnerabilidades independientes: el defecto común es ausencia total de enforcement.

Fuentes de código clave: `app/main.py` (creación DB y registro módulos 57–97; startup 245–258; handlers; fallback 2079–2086), `app/migrations.py` (ledger/runner 489–496; múltiples `executescript`), `app/modules/attachments.py` (router y multipart), `app/modules/decision_runtime.py` (registro opcional de `/api/decisions`), `Dockerfile`, `docker-compose.yml`, `docker-compose.portainer.yml`, `.env.example`.

Contratos revisados: `prototypes/admin_auth.py`, `admin_auth_http.py`, `admin_auth_cli.py`, `admin_persistence.py`, `http_policy_inventory.py`, `http_authorization_guard.py`. Los prototipos son aislados, no importados por `app/`; el CLI 1R exige `--db`, TTY para mutaciones y rechaza deliberadamente `nexo.sqlite3`; persistencia 2B.2 permite sólo fixtures temporales; guard 2B.3 exige snapshot y callbacks inyectados. Ninguno es autoridad operativa para NEXO, ni verifica identidad de servicio entre procesos.

El inventario 2B.1 de 75 bindings sigue coincidiendo con el censo HTTP actual bajo los dos módulos solicitados. La diferencia aritmética documentada es explícita: el snapshot 2B.1 reportó 75 HTTP + un pseudo binding `MOUNT`; el recuento independiente de 67 rutas app + 8 bindings framework produce 75 HTTP, y el mount se registra aparte. Las políticas abajo son **objetivo**; comportamiento actual de todos los endpoints API es anónimo. Los routers FastAPI de login del prototipo no forman parte del recuento.

Software Docs: workspace `Osviel Workspace`, proyecto `Nexo Chat` (`01a11dd0-e435-7ba2-a02a-3fbe2e27a54a`). Shared revisado: `architecture/overview.md` rev. 2, `security-administration.md` rev. 1 y `chat-request.seq` rev. 2 (cero diagnósticos). Documentan el mismo límite repo/prototipo/desconocido y citan la base anterior `08f18cd`; no se encontró contradicción de este HEAD. Propuestas existentes incluyen un refresh abierto y otro supersedido; no se creó MY WORK ni propuesta duplicada, ni se escribió Shared. Limitación: Shared no es snapshot del HEAD actual ni evidencia de instalación productiva.

## 3. Matriz HTTP auditada

Convenciones: `S` = sesión humana; `Admin(p)` = admin con permiso indicado; `User(p)` = usuario con permiso; ownership siempre es verificación independiente en DB. `CSRF` requerido en mutaciones basadas en cookie; sólo GET/HEAD no CSRF. Todas las filas hoy son `anónimas, sin policy`. “Test” nombra el mínimo caso de aceptación más la denegación anónima/permiso insuficiente, que debe verificar cero side effects. La policy de permisos es propuesta; permisos finales pendientes. Pese al objetivo MVP admin-only, preferencias pasan a usuario cuando se particionen; recursos conversacionales requieren ownership.

| Método y ruta | Propietario | Operación / efectos | Política objetivo; permiso | Ownership antes del handler | CSRF | Riesgo / test de aceptación requerido |
|---|---|---|---|---|---|---|
| GET, HEAD `/openapi.json` | FastAPI | Entrega schema | Deshabilitar o Admin(diagnostics:read) | No | No | Enumera superficie; test anónimo denegado y schema no cacheado |
| GET, HEAD `/docs` | FastAPI | UI Swagger | Deshabilitar o Admin(diagnostics:read) | No | No | Revela API; test anónimo denegado |
| GET, HEAD `/docs/oauth2-redirect` | FastAPI | Callback docs | Deshabilitar o Admin(diagnostics:read) | No | No | Ruta generada no omitida; test inventario |
| GET, HEAD `/redoc` | FastAPI | UI ReDoc | Deshabilitar o Admin(diagnostics:read) | No | No | Revela API; test anónimo denegado |
| POST `/api/files` | módulo `attachments` | Lee multipart, extrae PDF/DOCX/texto o devuelve imagen | User(chat:write) | No resource id; autorización antes de body parsing | Sí | FastAPI puede parsear `UploadFile=File` antes del endpoint; middleware ASGI debe denegar sin leer body. Test contador `receive`, extractor y memoria en cero; límite upload |
| POST `/api/decisions` | `decision-runtime` opcional | Valida/llama provider Arbiter | **Admin(diagnostics:run)** (MVP fijo) | No | Sí | Egress/coste; policy no derivada del manifest. Test admin requerido y provider mock no invocado al denegar |
| POST `/api/settings/retrieval-benchmark` | core | Resuelve notebook y lanza job/cómputo | Admin(diagnostics:run) | Sí: notebook si el producto lo hace de usuario; MVP admin-only | Sí | Resolver antes de benchmark; denied => job count 0 |
| GET `/api/settings/retrieval-benchmark/{job_id}` | core | Lee estado/resultado job | Admin(diagnostics:read) | Job ownership/actor si no es admin-only | No | IDOR de job; test foreign job hidden |
| GET `/api/settings/tools` | core | Lee runtime settings y presencia API key AEMET | Admin(config:read) | No | No | Metadata sensible; anon no DB read |
| PUT `/api/settings/tools` | core | Mutación settings/credencial AEMET | Admin(config:write) | No | Sí | Credencial persistida; denied => DB unchanged |
| GET `/api/mcp/servers` | core MCP manager | Lista endpoints/tools y presencia auth | Admin(mcp:read) | No | No | Topología interna/secret metadata; no bearer token retornado |
| POST `/api/mcp/servers` | core MCP manager | Persiste URL/credencial | Admin(mcp:write) | No | Sí | SSRF/egress y secreto; denied => no fila, test URL validation independiente |
| PUT `/api/mcp/servers/{server_id}` | core MCP manager | Cambia endpoint/token/config y sincroniza tools | Admin(mcp:write) | ID global admin; ownership no | Sí | SSRF/credencial; mismo transaction + no egress inesperado |
| PATCH `/api/mcp/servers/{server_id}` | core MCP manager | Habilita/deshabilita servidor | Admin(mcp:write) | ID global admin; ownership no | Sí | Puede activar tools/egress; denied => unchanged |
| POST `/api/mcp/servers/{server_id}/connect` | core MCP manager | Conexión externa y descubrimiento | Admin(mcp:connect) | Server existence/config bajo admin | Sí | SSRF/DoS; denied antes de `refresh`, egress count 0 |
| PATCH `/api/mcp/servers/{server_id}/tools` | core MCP manager | Activa/desactiva todas tools | Admin(tools:write) | Server scope; admin global | Sí | Cambia superficie ejecución; denied => unchanged |
| PATCH `/api/mcp/servers/{server_id}/tools/{tool_id:path}` | core MCP manager | Cambia estado/acción de tool | Admin(tools:write) | Tool pertenece al server | Sí | Path converter debe seguir matching; test tool de otro server 404 |
| DELETE `/api/mcp/servers/{server_id}` | core MCP manager | Borra server/credenciales por cascade | Admin(mcp:write) | ID global admin | Sí | Destrucción de configuración; delete y cascades sólo autorizado |
| POST `/api/interactions/{interaction_id}` | core | Resuelve interacción pending y desbloquea run | User(agents:interact) | **Sí**, run/conversation + owner; antes del CAS | Sí | Ahora muta `BEGIN IMMEDIATE`, resuelve por UUID; query/CAS con owner y estado en misma transacción; carreras: un ganador |
| GET `/api/preferences` | core | Lee preferencias globales actuales | User(config:read) | Usuario actual tras particionar | No | Hoy global/perezosa; no usar antes de auth; test dos usuarios aislados |
| PATCH `/api/preferences` | core | Escribe preferencias globales | User(config:write) | user id ligado a upsert/delete SQL | Sí | Hoy global; CAS/UPSERT por `(user_id,key)` y CSRF |
| GET `/api/health` | core | Health/version y ping Qdrant | Pública sólo liveness `{status}` | No | No | Hoy revela versión/errores Qdrant y hace red; test sin DB/Qdrant, payload mínimo; readiness privada |
| GET `/api/settings/embeddings` | core | Lee config providers/estado e index health | Admin(config:read) | Ninguno, admin global | No | Discloses providers/config; test no provider/read anónimo |
| PUT `/api/settings/embeddings` | core | Crea versión config, marca índices outdated | Admin(config:write) | Ninguno, admin global | Sí | DB multiwrites; transacción cubre validaciones y escrituras; denied/config inválida sin alteración |
| POST `/api/settings/embeddings/test` | core | Egress provider para embedding | Admin(providers:test) | Ninguno, admin global | Sí | Egress/coste; mock confirma cero llamadas al negar |
| POST `/api/settings/embeddings/test-reranker` | core | Envía query/documentos sintéticos a provider | Admin(providers:test) | Ninguno, admin global | Sí | Egress y entrada; mock; prueba consulta jamás enviada al negar |
| PATCH `/api/providers/{pid}/models/{model_id:path}` | core | Cambia capabilities modelo | Admin(providers:write) | Provider-model composite id | Sí | Afecta autorización efectiva de tools; UPDATE condicionado `pid,id`, permisos y test cruzado |
| GET `/api/modules` | core registry | Lista módulos/capacidades/estado | Admin(config:read) | No | No | Información de despliegue; no efecto |
| GET `/api/tools` | core registry | Lista catálogo de herramientas | Admin(tools:read) | No | No | Describe acciones disponibles; deny antes catálogo |
| GET `/api/agents` | core AgentProfiles | Lista perfiles/config/instrucciones | Admin(agents:read) | Admin global MVP; notebook links revisados | No | Instrucciones internas; no anon |
| POST `/api/agents` | core AgentProfiles | Crea perfil/links tools-notebooks | Admin(agents:write) | Validar cada notebook/profile ref bajo policy | Sí | Global, puede elevar config tools; deny => sin row/link |
| GET `/api/agents/{profile_id}` | core AgentProfiles | Lee perfil/instrucciones | Admin(agents:read) | Perfil global admin | No | IDOR si luego personal; test |
| PATCH `/api/agents/{profile_id}` | core AgentProfiles | Cambia perfil/allowlist/provider/notebooks | Admin(agents:write) | Validar refs y relaciones de forma atómica | Sí | Puede ampliar tool/data access; denied no update |
| DELETE `/api/agents/{profile_id}` | core AgentProfiles | Borra perfil | Admin(agents:write) | Perfil global admin | Sí | Refs/cascades; test snapshot en ejecución |
| GET `/api/notebooks` | core NotebookService | Lista notebooks | Admin(rag:read) MVP; User(chat:read) después | owner en SELECT | No | Hoy lista global; count/data no leak cross-owner |
| POST `/api/notebooks` | core NotebookService | Crea notebook | Admin(rag:write) MVP; User(rag:write) tras owner | Owner asignado del principal en INSERT | Sí | Owner no tomar del body; INSERT owner transaction |
| GET `/api/notebooks/{notebook_id}` | core NotebookService | Lee notebook | Admin(rag:read)/User(rag:read) | **Sí** `id AND owner_user_id` en SELECT | No | UUID IDOR; ajeno/inexistente misma 404 |
| PATCH `/api/notebooks/{notebook_id}` | core NotebookService | Cambia metadata | Admin(rag:write)/User(rag:write) | **Sí**, mismo UPDATE condicionado | Sí | TOCTOU; CAS rowcount test |
| DELETE `/api/notebooks/{notebook_id}` | core NotebookService | Borra notebook/resources por cascade | Admin(rag:write)/User(rag:write) | **Sí**, DELETE con owner | Sí | Pérdida de datos/index; cross-owner unchanged |
| GET `/api/notebooks/{notebook_id}/sources` | core NotebookService | Lista fuentes | Admin(rag:read)/User(rag:read) | **Sí** notebook owner en SELECT/join | No | No leer resource antes de gate |
| GET `/api/notebooks/{notebook_id}/sources/{source_id}` | core NotebookService | Lee fuente | Admin(rag:read)/User(rag:read) | **Sí**, fuente bajo notebook propio en misma consulta | No | Parent-child IDOR test |
| GET `/api/notebooks/{notebook_id}/sources/{source_id}/document` | core NotebookService | Devuelve documento canónico | Admin(rag:read_content)/User(rag:read_content) | **Sí**, owner y source parent | No | Texto sensible; test body del otro owner no entregado |
| GET `/api/notebooks/{notebook_id}/sources/{source_id}/index` | core + vector index | Lee index identity/count/config | Admin(rag:read)/User(rag:read) | **Sí**, notebook/source owner | No | Filtra metadata; pruebas admin y owner |
| GET `/api/notebooks/{notebook_id}/sources/{source_id}/integrity` | core retrieval | Diagnóstico index/doc | Admin(rag:diagnostics) | **Sí**, antes de consultas/cómputo | No | Contenido derivado/CPU; no query al denegar |
| POST `/api/notebooks/{notebook_id}/sources/{source_id}/ingest` | core ingestion | Extrae canonical y persiste | Admin(rag:write)/User(rag:write) | **Sí**, owner en escritura/source lookup | Sí | CPU/file effects; idempotencia y owner SQL |
| POST `/api/notebooks/{notebook_id}/sources/{source_id}/index` | core retrieval | Embedding egress y mutación índice | Admin(rag:write)/User(rag:write) | **Sí** antes del egress; validar de nuevo al persistir | Sí | Provider/DB/vector side effects; mock cero al denegar |
| POST `/api/notebooks/{notebook_id}/retrieve` | core retrieval | Embedding/provider query y devuelve texto | Admin(rag:query)/User(rag:query) | **Sí** antes de embedding y en retrieval SQL | Sí | Exfiltra chunks; test no embedding/provider/resultado cross-owner |
| POST `/api/notebooks/{notebook_id}/retrieve/diagnostics` | core retrieval | Retrieval y rankings/matches | Admin(rag:diagnostics) | **Sí** antes de embedding y query | Sí | Filtra candidatos; denegación egress=0 |
| POST `/api/notebooks/{notebook_id}/sources` | core ingestion | Lee multipart o JSON URL; agrega archivo/web | Admin(rag:write)/User(rag:write) | **Sí** autorización antes de body; notebook owner en insert | Sí | Multipart body parsing y SSRF URL; middleware body-read test, URL egress policy |
| DELETE `/api/notebooks/{notebook_id}/sources/{source_id}` | core ingestion | Borra source/document/chunks/index | Admin(rag:write)/User(rag:write) | **Sí**, DELETE con parent owner atómico | Sí | Cascada destructiva, external vector cleanup; cross-owner no-op |
| GET `/api/lab/shadow` | core diagnostics | Lee respuestas/observaciones por conversación | Admin(diagnostics:read) | MVP admin; si user, filtrar conversation owner en SQL | No | Answers/metadata sensible; test filtros y datos anon |
| GET `/api/lab/diagnostics` | core logger | Lee logs diagnósticos recientes | Admin(diagnostics:read) | Admin global | No | Puede contener datos; redaction + no anon |
| GET `/api/lab/traces` | core traces | Lee runtime y legacy traces | Admin(diagnostics:read) | **Sí** si filtra conversation/message/run; SQL owner en cada rama legacy | No | Parámetros combinables; no bypass por `message_id` o fallback legacy |
| GET `/api/lab/runs` | core runs | Lista runs | Admin(diagnostics:read) | **Sí** por conversación en SQL si user; run id no bearer | No | Test foreign IDs |
| GET `/api/lab/runs/{run_id}` | core runs | Lee run/events | Admin(diagnostics:read) | **Sí**, join conversation owner antes de eventos | No | UUID IDOR; no events leídos si ajeno |
| GET `/api/lab/runs/{run_id}/events` | core runs | Lee events | Admin(diagnostics:read) | **Sí**, igual run parent | No | Hereda gate del run; prueba directa |
| GET `/api/lab/traces/{message_id}` | core traces | Lee traces runtime/legacy | Admin(diagnostics:read) | **Sí**, owner en ambos queries | No | Fallback legacy debe filtrar; test |
| GET `/api/providers` | core providers | Lista metadata/models/key-present | Admin(providers:read) | Admin global | No | Credential metadata; key jamás retornada |
| POST `/api/providers` | core providers | Persiste URL/API key/modelos | Admin(providers:write) | Admin global | Sí | SSRF y secret; no egress al guardar; denial DB unchanged |
| PUT `/api/providers/{pid}` | core providers | Cambia URL/key/modelos | Admin(providers:write) | Admin global, conditional row + referencias | Sí | Secret update; transaction y URL validation independiente |
| DELETE `/api/providers/{pid}` | core providers | Borra provider/modelos en cascada/restricción | Admin(providers:write) | Admin global | Sí | Integridad FK y referencias; test fail safe |
| POST `/api/providers/{pid}/refresh` | core provider client | GET remoto `/models`, persiste capacidades | Admin(providers:test) | Provider config validado antes egress | Sí | SSRF, credenciales/redirect; mock no request si denegado |
| DELETE `/api/providers/{pid}/models/{model_id:path}` | core providers | Borra modelo | Admin(providers:write) | Composite provider/model | Sí | Puede invalidar config; delete condicional y test |
| GET `/api/conversations` | core conversations | Lista conversaciones y metadata | User(chat:read) | **Sí** `owner_user_id` en SELECT | No | Hoy lista toda la instancia; test isolation |
| GET `/api/conversations/{cid}` | core conversations | Lee mensajes, citas/excerpts y approvals | User(chat:read) | **Sí** propietario en primer SELECT antes joins/citas | No | Alto impacto de datos; recurso ajeno 404 antes de query descendiente |
| POST `/api/approvals/{approval_id}` | core approvals | Consume approval, ejecuta tool, reanuda agente/SSE | User(tools:approve) | **Sí**, approval-run-conversation-owner comprobado en misma transacción que CAS `pending -> ...` | Sí | Hoy `resolve_approval()` y `executing` separados; TOCTOU/replay/execution before ownership; single-winner test y zero tool/provider al denegar |
| DELETE `/api/conversations/{cid}` | core conversations | Borra conversación y descendientes | User(chat:delete) | **Sí**, DELETE condicionada por owner y rowcount | Sí | Destrucción cascada; race/idempotencia y cross-owner |
| PATCH `/api/conversations/{cid}` | core conversations | Cambia título/profile/notebook/toggles | User(chat:write) | **Sí**, owner en read/UPDATE transaction; references notebook/profile autorizadas | Sí | Cross-owner references y TOCTOU; atomic test |
| POST `/api/conversations/{cid}/branch/{message_id}` | core conversations | Copia mensajes y citas a conversación nueva | User(chat:write) | **Sí** parent owner; INSERT rama hereda el mismo owner en transacción | Sí | Hoy copia citas sin owner; rollback completo si falla |
| POST `/api/chat` | core chat/AgentRuntime | Crea/continúa conversación, inserta mensaje/run, hooks/task shadow; devuelve SSE con RAG/provider/tools y persistencia | User(chat:write), CSRF; además rag:query si RAG y scopes tool | **Sí antes de BEGIN/lecturas**, create owner en INSERT; existente SELECT `id AND owner`; profile/notebook/provider dentro del boundary permitido | Sí | Orden crítico: FastAPI valida/parsea `ChatIn`; handler `BEGIN IMMEDIATE` y cambia/lee DB, inserta user message+run antes de responder; hooks y `create_task` antes de `StreamingResponse`; productor efectúa RAG/provider/tool y persistencias. Test anónimo/inválido => cero rows/hook/task/egress; excepción antes SSE; revalidación cada efecto |
| MOUNT `/assets` | `StaticFiles` | Sirve assets bajo mount | Pública explícita | No | No | sólo directorio web estático; prueba no exponer DATA_DIR, symlinks/paths privados, cache adecuado |
| GET `/{path:path}` | core SPA fallback | Sirve archivo web o index para cualquier path | Shell pública sólo para paths UI allowlisted; **no policy pública wildcard** | No | No | Hoy `/api/no-existe` puede devolver index HTML 200; cerrar `/api/*` con 404, negar path traversal y no servir ficheros fuera web |

La matriz cubre chat/SSE, tools, MCP, AgentProfiles, providers/secrets, RAG/notebooks/archivos, approvals/interactions, diagnostics/lab, settings, rutas framework y rutas/mounts de módulos. No se observan WebSockets ni MCP HTTP route bajo el módulo `mcp`; MCP administra conexiones desde API core. Los assets interiores de `StaticFiles` no se enumeran como rutas individuales: su frontera de mount es la policy; comprobar que `web/assets` no incluya contenido privado.

### Rutas públicas: límite deliberado

- `GET /api/health`: puede ser pública sólo como liveness mínimo y barato `{status:"ok"}` sin versión, DB, Qdrant, excepción o readiness. Hoy hace ping al vector store y publica detalle; no cumple ese contrato. Readiness operativa detallada debe ser privada o protegida por canal de monitorización explícito.
- `MOUNT /assets`: público porque son assets estáticos de interfaz, no datos/credenciales; aislar directorio de cualquier storage de usuario.
- `GET /` y rutas UI explícitas: shell público no equivale a API pública; UI no concede acceso. El catch-all actual no es admisible como boundary pública.
- `POST /api/auth/login` (futuro): necesariamente accesible antes de sesión, pero sólo con TLS/Host/Origin exactos, rate limit durable, respuesta uniforme/no-store y sin bootstrap/recovery. No forma parte del runtime actual.
- `POST /api/decisions` no es público: admin(diagnostics:run) MVP fijo. Módulo opcional/capability no autentican principal.
- Todo otro `/api/*`, incluidas rutas API no reconocidas: autenticación según policy si existe y 404 si no existe. Sin wildcard público.

## 4. Ejecución previa a autorización y TOCTOU

### Orden observado

1. Import de `app.main` calcula `DATA_DIR`, crea directorio y `DB_PATH`, registra native tools, módulos según `NEXO_MODULES`; MCP manager siempre se registra. `AttachmentsModule.register()` usa `include_router`; `DecisionRuntimeModule.register()` añade ruta con `context.app.post`. Rutas framework y catch-all aparecen en la misma app. No hay guard al importar.
2. Startup sincronizado por `@app.on_event` hace `db()`, `migrate(c)`, luego `module_registry.startup()` y lanza actualización asíncrona de proveedores. No se instala/valida un inventario y la app no comprueba auth_state/policy/ownership. `embedding_configuration()` y `knowledge_health()` llaman también `migrate()` desde handlers.
3. FastAPI resuelve path, valida/convierte body y dependencies antes del endpoint. Por tanto, depender sólo de `Depends` puede ser demasiado tarde para `UploadFile=File(...)` y no cubre uniformemente rutas/framework/mounts; guard debe ser middleware ASGI exterior que resuelva método/ruta/policy y autorice sin consumir body antes de pasar al router.
4. `/api/chat` usa `ChatIn`: el endpoint abre `BEGIN IMMEDIATE`, consulta/actualiza conversación, resuelve notebook/profile/provider/model, guarda mensaje y run. Después del commit hace hook y puede crear tarea de shadow; recién retorna `StreamingResponse`. RAG, llamadas provider/model, herramientas, interacción/approval y resultados aparecen en generador. Una denegación SSE después del inicio es HTTP 200 y no revierte estos actos.
5. `/api/approvals/{id}` consume/decide approval, transiciona a `executing`, lee provider y llama herramienta antes de devolver stream de continuación. Consentimiento UUID no autentica usuario; la verificación de status/fingerprint no acredita ownership/permiso actual.
6. `/api/interactions/{id}` abre transacción y lee el recurso por id, comprueba pending/schema/expiración y actualiza one-shot. No valida caller ni conversation owner.
7. `/api/files` define `UploadFile=File(...)`; `/notebooks/.../sources` parsea `request.form()` y lee hasta límite en el endpoint; /sources JSON recibe URL que inicia fetch en fase posterior. Auth debe preceder body, extracción, URL fetch y persistencia.

### Ubicación y responsabilidades del guard

Integrar una envoltura ASGI exterior al router sólo después de un registro completo, incluídas rutas framework y módulos habilitados. Validar manifest estricto dentro del lifespan antes de que Uvicorn acepte tráfico; fallar startup/readiness en cualquier ruta/método/policy desconocidos. El diseño 2B.3 por-request fingerprint puede fallar 503 ante drift, pero el prototipo no congela registro ni conecta readiness. Evitar registro tardío después del gate.

El guard resuelve policy, session, permiso, Host/Origin/CSRF/content-type y ownership inicial antes del endpoint/body; los endpoints/servicios aún deben exigir autorización de dominio. Callback ownership previo **no** es garantía: SELECT + UPDATE separado deja TOCTOU. Para cada operación privada el predicado de owner debe estar en el mismo SQL/transaction que lectura o mutación (`... WHERE id=? AND owner_user_id=?`); para transiciones one-shot CAS `UPDATE ... WHERE status='pending' AND ...owner...`, con rowcount exacto y sesión/permiso revisados dentro del mismo límite transaccional. No confiar en resource id, profile/tool allowlist o delegación del cliente.

### Límites transaccionales/CAS a resolver

- Crear chat: mismo `BEGIN IMMEDIATE` para verificar auth/session/policy vigente, crear conversation con owner, validar referencias autorizadas, insertar mensaje/run; única respuesta a request duplicado/carrera según run status.
- Continuar/branch: ownership del origen y asignación del owner de branch dentro de misma transacción; notebook/profile referenciados deben ser visibles al mismo actor.
- Approval: resolver estado + dueño en join y transición CAS a `executing` atómicos; ejecutar tool fuera de la transacción larga; antes del efecto, revalidar sesión/delegación, owner/run activo, fingerprint, args hash y scope; guardar terminal state condicionado y consistente. Ningún fallo de persistencia debe permitir continuar efecto ni marcar éxito sólo en memoria.
- Interaction: misma idea CAS con owner, run vigente, schema/expiry y autorización en una transacción; una carrera produce un ganador, no replay.
- Notebook/source delete/update: parent ownership y write en SQL bajo misma transacción; efectos externos/index cleanup requieren outbox/reconciliación o repetición idempotente antes de afirmar atomicidad cross-store.
- Sesión/logout/reset: revocación, evento y cambio de credencial/estado en mismo SQLite transaction. No mantener DB transaction durante scrypt, egress, proveedor o stream.
- Policies/permission: revocación/grants en SQLite deben tener punto de linearización. Auth HTTP preliminar puede expirar entre handler y efecto; autorización del efecto comprueba la revisión/session actual.

### Streaming y delegación

Revalidar al menos: request HTTP antes de any DB mutation/run/task/provider; inmediatamente antes de enviar `http.response.start` para cubrir revocación entre handler y primer byte; antes de cada embedding/retrieval, provider/tool/MCP, creación de interacción/approval, persistencia de resultados y reanudación. Comprobar también run active/cancel/revoked, session revocation, owner, audience/scope, expiración, herramienta/snapshot y permiso en cada efecto. Revocación durante un proveedor bloqueado es cooperativa y no puede deshacer un efecto ya comenzado; documentar latencia máxima y cancelación cooperativa. Nunca traducir denial inicial a un SSE event.

El helper 2B.3 `authorize_effect` sólo cubre contrato de fixture y callbacks; no está invocado por el productor de `app.main`. El service principal es objeto Python, no identidad workload. Si threat model exige protección frente a runtime comprometido, el guard in-process no basta; decidir proceso/UID/IPC antes de prometer aislamiento de tools mutativas.

## 5. Persistencia, esquema y migraciones

### Estado observado

`app/migrations.py` mantiene versiones consecutivas 1–28; `_migration_28` añade `mcp_tools.read_only_hint`; `schema_migrations(version PRIMARY KEY, applied_at)` es ledger. `migrate()` crea ledger, obtiene versiones aplicadas y corre las pendientes en orden; tras cada callable inserta versión. No valida explícitamente ledger gap/versión futura, consistencia de todos objetos o checksum. El esquema de repo actual **no contiene auth users/sessions/perms, `owner_user_id` ni auth_state**. `user_preferences(key PK,value,updated_at)` se crea lazy en runtime, fuera del ledger. DB productiva no abierta: su versión/esquema desconocidos.

Migraciones 1,2,3,5,6,8,9,10,12,18,19,22,23,26,27 incluyen `executescript` (además de DDL/DML con `execute`). Con `sqlite3`, `executescript()` puede hacer commit implícito de una transacción pendiente antes de ejecutar script; la transacción implícita/conexión/context manager no debe tomarse como garantía de rollback del conjunto. La migración 12 incluye virtual table FTS5 y backfill; no asumir soporte idéntico en todo SQLite real. No hay `PRAGMA journal_mode=WAL` ni `user_version` configurado en `db()`; se habilita/verifica `foreign_keys=ON`, sin `busy_timeout` explícito en conexión de aplicación. SQLite default journal mode depende de archivo/configuración. El contexto `with sqlite3.Connection` gestiona commit/rollback de DML transaccional, pero no cierra conexión y no neutraliza commit implícito de `executescript`.

Ruta de apertura observada: `app.main` crea DATA_DIR al importar y `db()` abre `DB_PATH`; startup llama migrate. Además hay `migrate()` dentro de handlers para lectura/configuración RAG. Sin auth_state gate podría seguir atendiendo incluso si inicialización administrativa falla en ruta posterior. No se leyó archivo DB, ni se inspeccionó data dir del despliegue.

### Evolución objetivo (propuesta, no SQL ni migración creada)

- Tablas versionadas `auth_schema_version`/singleton `auth_state` fail-closed, `auth_users`, `auth_user_permissions` (allowlist explícita, sin `*`), `auth_sessions` (sólo hash token/CSRF, expiry idle/absolute/revoke), `auth_login_guard` persistente y `auth_security_events` allowlisted/redacted. Constraints/FK/índices y policy_version explícitos.
- `conversations.owner_user_id`, `notebooks.owner_user_id` con FK/índices. Crear recursos con owner en mismo INSERT; source/document/chunk/citation heredan por parent. Runs, events, interactions, approvals y traces filtran por conversation/run parent. `user_preferences` evoluciona a `(user_id,key)` y valores globales existentes se copian al primer admin, preservando timestamp/valor.
- Providers/modelos/secrets, MCP/AEMET, AgentProfiles, módulos y settings permanecen globales/admin en MVP; admin no significa ACL de filesystem. No cifrar secretos a partir de hash de password; file/backups siguen siendo sensibles.
- Bootstrap atómico: sólo tras revisión/read-only schema audit y backup; en `BEGIN IMMEDIATE`, crear admin/grants, ownership histórico únicamente si operador autoriza, mover preferencias, invalidar approvals, cancelar/expirar interacciones y runs pendientes, verificar FK/integrity/owners y cambiar `auth_state.initialized=1` al final. No servir tráfico privado antes del commit.
- Toda incompatibilidad, ledger futuro/gap, objeto parcial, FK roto, owner nulo/huérfano inesperado, lock/storage error o falta auth state => fail closed (no listener privado; no fallback anónimo).

### Atomicidad y evidencias exigidas

**El runner actual no permite afirmar que la migración completa sea atómica.** El adapter 2B.2 sí demuestra transacciones `BEGIN IMMEDIATE` con sentencias `execute` en DB temporal, pero su fixture rechaza el archivo productivo y no modifica `app/migrations.py`. Sus pruebas de trigger/exception no equivalen a matar proceso/host. La prueba backup WAL con writer concurrente demuestra consistencia para ese escenario local controlado; no prueba crash/power loss, storage productivo ni restore de Portainer.

Antes de cualquier migración productiva: autorización y auditoría **read-only** del archivo/volumen productivo (no realizada aquí ni abrir/copiar SQLite en este checkpoint), preferiblemente con acceso offline autorizado/SQLite online backup a entorno segregado en un futuro checkpoint. Registrar sólo versión/ledger, `sqlite_master`, `table_info`, FK, integrity, conteos/checksums sin contenido ni secretos; confirmar SQLite runtime/FTS5/constraints, journal mode, WAL/sidecars, tamaño, busy writers y path/mount. Comparar varias formas legacy reales. No migrar si hay filas huérfanas, esquema divergente o ledger desconocido.

Demostraciones exigidas en entorno descartable y copia representativa, bajo proceso harness reproducible:

1. Inyectar error SQL en cada límite/fase, assert rollback de DDL/DML/ledger, integridad, FKs y que auth sigue uninitialized; contrastar `executescript` actual contra la estrategia futura sin afirmar comportamiento por suposición.
2. Terminar proceso abruptamente (SIGKILL) antes/durante/después de cada fase y recuperar/rearrancar repetidamente: no estados parcialmente activados, login imposible antes del bootstrap completo y una migración reejecutable/recuperable determinísticamente.
3. Reiniciar/recrear contenedor conservando `nexo-data`, y repetir recovery; verificar ledger/objetos/ownership/state y que readiness permanece cerrada ante incomplete/unknown.
4. Verificar consistencia de versión: esquema esperado ↔ ledger sin lag/adelanto, gaps ni objeto parcial; validación fail closed de schema futuro/desconocido.
5. Restaurar con SQLite Backup API desde fuente WAL coherente en staging; comprobar `integrity_check`, `foreign_key_check`, conteos/checksum no sensibles y login/bootstrap state esperado; publicar restore sólo en destino nuevo y preservar backup inmutable.
6. Comparar datos históricos antes/después: providers/keys (sin imprimir), conversations/messages/citations, notebooks/documents/chunks, profiles, approvals/interactions/runs y preferencias. Sin borrados/cambio de contenido; owner assignment sólo con aprobación firmada y reporte de conteos; Qdrant no se altera y coherencia de snapshots se evalúa separadamente.
7. Asegurar que DB post-migración no permita `initialized=1` sin admin/grants/owners/audit válidos; falla parcial no emite cookie ni habilita handler privado. Mantener claves/contraseñas fuera de logs y reportes.

No editar migraciones publicadas retrospectivamente ni asignar aquí el número próximo. Sólo revalidar HEAD/lista y seleccionar versión en checkpoint autorizado. Preferir ejecutar migración offline con writers detenidos y no dentro del arranque de una app expuesta; si startup la ejecuta, listener/ingress y readiness deben estar cerrados hasta validación completa.

## 6. Bootstrap, operador y recuperación

El CLI 1R es aislado, no entrypoint instalado y sólo opera su propio store. `--db` explícito, TTY para cambios, `getpass`, permisos parent/UID y rechazo de basename/path NEXO son guardrails intencionales. 2B.2 `AdministrativePersistence` confina paths a temp root y rechaza `nexo.sqlite3`; **no modificar esa protección para eludirla**. No usar `admin_auth_cli` sobre DB productiva aunque se cambie el nombre del fichero.

Integración requerirá herramienta host-side nueva y auditada (no diseñada/implementada aquí), distinta del CLI aislado: operador autenticado por identidad del host/OS, sesión de mantenimiento aprobada, binario/paquete confiable, `--data-dir`/mount inequívoco con resolución real symlink y dispositivo/inode, comprobar UID/ACL/permisos, lock/exclusión del web y writers, versión/esquema read-only, backup/restore verificado, TTY/no secretos argv/env/logs, transacción, salida sanitizada y auditoría no secreta. Determinar explícitamente host vs contenedor: preferir host-side con volumen detenido/montado y ningún socket Docker; no instalar admin CLI en el servicio web. Si contenedor offline por operador, UID distinto de root, path exacto y exclusión de app obligatorios. Protección POSIX no resiste root/host/Portainer owner.

Runbook futuro, sin endpoint recovery: cerrar ingress y detener runtime; autenticar operador por mecanismo host confirmado; validar path/volume/schema/backup; procedimiento local TTY para cambiar hash y revocar todas las sesiones/evento en transacción; repetir verify/status/login controlado; arrancar cerrado, probar permisos; reabrir sólo con aprobación. Pérdida de acceso se trata como recovery con dos operadores o proceso break-glass aprobado según política del host, nunca contraseña universal. Sesiones comprometidas: revocar global/localmente desde herramienta host, rotar password y revisar logs/backups/secrets; no asumir que revocar cookie detuvo stream ya ejecutado. Bootstrap incompleto, archivo ausente/corrupto o migración fallida: no reintentar claim ni recrear estado ambiguo; ingress cerrado, preservar evidencia/DB, restaurar backup compatible en destino separado y reparar por runbook firmado. No hay bootstrap HTTP, usuario/password default ni bypass permanente.

Decisiones abiertas: identidad/autorización del operador, quién puede acceder al mount Portainer, autenticación de dos personas para recovery, audit retention, host/container execution, custodia/cifrado backup, ownership histórico, KDF hardware y lockout/DoS.

## 7. Topología y despliegue

### A. Demostrable en repo

- `Dockerfile`: `python:3.12-slim`, no declara `USER`, Uvicorn `--host 0.0.0.0 --port 8787`; por defecto proceso root. `EXPOSE 8787` no es firewall.
- `docker-compose.yml`: `8787:8787` sin bind address, `nexo-data:/data` y `qdrant-data`; sin healthcheck, TLS, proxy, networks privados explícitos, read-only FS, secrets o usuario. NEXO y Qdrant exponen sólo servicio Qdrant sin ports declarados aquí. DB key material está en SQLite/env.
- `docker-compose.portainer.yml`: mapeo host configurable `NEXO_HTTP_PORT:-8787:8787`, named volumes, sin healthcheck/TLS/proxy/user explícito; env configura integraciones. No demuestra Portainer real activo.
- Uvicorn 0.0.0.0 escucha todas las interfaces del contenedor; Compose publicado host port puede ser alcanzable según host firewall/topología. No existe middleware Host/TrustedHost, proxy trust, CORS/CSRF o cookie en runtime. `.env.example` no define auth/origin/proxy.
- No hay evidencia versionada de HTTPS; README instruye URL HTTP del host para uso local. No se encontró reverse proxy configurado.
- Data path `/data`; DB SQLite incluye API keys provider, bearer MCP y AEMET key sin cifrado aplicativo. Compose preserva named volume `nexo-data` y Qdrant separate.

### B. Instalación Portainer no demostrada

No se inspeccionaron Portainer, host, stack/env desplegados, estado de puertos, networks, UID efectivo, bind mounts/volúmenes efectivos, proxy, certificados, DNS, TLS termination, headers forwarded, VPN, firewall/ACL, acceso LAN/Internet, backups, snapshots Qdrant, logs, secret store ni despliegue running HEAD. No afirmar que una instalación tenga TLS/VPN/firewall/auth perimetral. Un puerto no publicado en uno de estos archivos no prueba ninguna regla exterior.

### Checklist de verificación operacional posterior

- Identificar host/stack, digest de imagen/commit, modo Portainer/Compose, usuario ejecutor y versiones SQLite/FTS5.
- Capturar `docker inspect` saneado: published port/interface, user, mounts reales, env keys presentes sin valores, networks y restart policy; no abrir DB.
- Verificar ingress desde host, LAN autorizada, VPN y red externa controlada; confirmar firewall/ACL; cerrar el puerto directo 8787 si se usa proxy.
- Confirmar proxy/TLS/FQDN/cert chain/renovación, Host allowlist, Origin exacto, forwarded headers sobrescritos y trusted proxy CIDR único; probar conexión directa spoof y redirect HTTP sin enviar secretos.
- Confirmar `Secure; HttpOnly; SameSite=Lax; Path=/; __Host-` cookie via browser real y CSRF móvil; no CORS wildcard; XSS/CSP en gate separado.
- Confirmar API/SSE sin buffering/cache, tiempo máximo/idle adecuado, límite body y tasa; logs no almacenan body, cookie, CSRF, Authorization, prompts/secretos; access logs redacted.
- Confirmar UID no-root, ACL data/backups, volumen persistente y plan de backup cifrado/restore; revisar Qdrant acceso autenticado/segmentación y snapshot cuando aplique.
- Confirmar observabilidad/alertas para auth failures, denied route, DB unavailable, migración/readiness; sin endpoint público que filtre detalle.

## 8. Activación cerrada por defecto

La integración debe entrar en modo `closed` sin excepción permisiva. No introducir flag “auth disabled” como recuperación. El guard no sirve rutas privadas mientras falte policy snapshot, auth state, ownership completo, esquema conocido, configuración Host/Origin/TLS o bootstrap. Readiness sólo puede subir tras gates completos; liveness puede mantenerse pública y no equivale a disponibilidad/autorización.

Secuencia propuesta:

1. **Preflight:** freeze de release/HEAD y módulos; censo de rutas/metodos (incluye docs, mounts, fallbacks, módulos opcionales); policy manifest versionado; confirmación operator de topology y owners históricos; auditoría read-only autorizada de DB/schema y estado; comprobar staffing/runbook.
2. **Ingress closed:** bloquear firewall/proxy/Portainer y detener app/writers. Si no se puede demostrar ingress cerrado, parar sin tocar DB.
3. **Backup:** backup SQLite consistente cifrado fuera del host/ACL separado; verificación restore en staging, integridad/FK, conteos y snapshot Qdrant compatible; conservar original read-only.
4. **Release preparado:** binario con enforcement completo y routes desconocidas 404, validación `strict` post-registration dentro lifespan; sin abrir puerto/ingress. Configurar proxy TLS/origin/trusted proxy pero mantener regla deny.
5. **Migración:** aplicar migración ensayada offline sobre DB/copias representativas; verificar ledger, integridad, FKs, datos y ownership requerido. Si error, parar: no arrancar versión sin auth sobre DB modificada en ingress abierto.
6. **Bootstrap:** comando host-side aprobado, TTY, admin inicial sin default password, grants allowlisted; ownership histórico sólo con autorización; reasignar preferencias; invalidar pendientes y terminalizar runs/interactions ambiguos; inicializar state al final en transacción. Releer/verify read-only.
7. **Arranque cerrado:** iniciar con ingress privado/local; startup falla ante schema desconocido, auth state incompleto, faltante de policy, DB indisponible o owner inconsistente. Liveness puede decir process alive; readiness privada debe decir ready sólo cuando inventory/policy/auth/DB y migraciones completas. Autorización efectiva se valida para cada request/efecto, no queda inferida de readiness. CP4 por sí solo nunca autoriza activar: deben estar completados y verificados CP5 y CP6 antes de solicitar GO de activación.
8. **Acceptance:** probar catálogo completo y cada método; anon/auth/permiso/CSRF/Origin/Host, API unknown 404 JSON, no fallback; IDOR y races; body-read counters; fake provider/tools sin secretos; SSE antes del start y revocation midstream; approvals/interactions replay; readiness/liveness y no-start failure. Validar browser/proxy real; registrar resultado sin PII/secret.
9. **GO operacional explícito:** dos personas/owner de release revisan gates, backup/restores, ingress real y decisión LAN/VPN/Internet. Abrir ingress de alcance mínimo, monitorizar. Si cualquier gate da desconocido, no abrir.
10. **Rollback:** cerrar ingress primero; detener writers; preservar DB/logs; diagnosticar. Recuperar código, esquema, datos y red como un conjunto compatible, desde backup/restauración ensayados y en mantenimiento; no combinar una versión de código con un esquema/datos incompatibles. Está prohibido restaurar o arrancar una versión antigua que vuelva a exponer endpoints anónimos, aunque se restaure el código sin la DB. No borrar auth tables ni hacer downgrade manual. Ante cualquier incertidumbre de compatibilidad o enforcement, mantener ingress cerrado. Reabrir sólo tras aprobar de nuevo el mismo checklist de activación.

Readiness: requisitos de comienzo seguro; liveness: proceso vivo solamente; authorization effective: toda petición/operación protegida con policy y owner actuales. `/api/health` 200 no satisface ninguna de las otras dos.

## 9. Matriz de bloqueos

| ID / severidad | Evidencia concreta | Impacto | Dependencias | Mitigación | Prueba de resolución | Checkpoint |
|---|---|---|---|---|---|---|
| C1 Crítico, bloquea implementación y activación | 75 bindings sin auth/policy; app no usa prototipos; rutas provider, data, tools y approval anónimas | Exposición de secretos/PII/ejecución y cambios | Catálogo y permisos aprobados | Enforcement integral deny-by-default en un release indivisible | ASGI matrix exhaustive; toda ruta private anon 401; cero side effects | 1, 4, 7 |
| C2 Crítico, bloquea implementación y activación | `/chat` persiste message/run y crea hooks/task antes SSE; approval hace tool antes de stream; uploads parsean body | Autorización parcial/HTTP 200 SSE/efectos tras denial | Guard, owner, delegado | Middleware pre-body y checks dominio pre-side-effect; reauthorize per-effect y pre-SSE | Contadores de DB/body/provider/tool/SSE; revocation gates y tests | 1, 4, 5, 6 |
| C3 Crítico, bloquea migración productiva | `migrate()` 1–28, `executescript`, sin atomicity/crash proof | Esquema/ledger parcial, datos/auth incoherentes | Contrato migrador y DB matrix | Migración offline atómica demostrada o startup estrictamente cerrado; no asumir executescript | SQL failure + kill/restart matrix y consistency checks | 2, 7 |
| C4 Crítico, bloquea cualquier activación productiva | No se abrió/inspeccionó DB real ni backup/version | Aplicar schema/ownership a estado erróneo o pérdida irrecuperable | Autorización operador y entorno | Read-only audit DB por instalación; backup y restore validado | Evidencia firmada de schema/ledger/foreign/integrity/counts y restore | 2, 7 |
| C5 Crítico, bloquea bootstrap/activación | CLI aislado rechaza DB NEXO; no hay host-side admin tool para schema principal | Sin bootstrap/reset seguro, tentación de endpoint/bypass | DB target y host operation | Diseñar herramienta host-side específica, no tocar guard del prototipo | TTY/operator/path/UID/backup/transaction tests | 3, 7 |
| A1 Alto, bloquea user policy/activación | v28 no tiene owner; API lee conversaciones/notebooks global | IDOR/cross-user leakage | Decisión ownership histórico y endpoints | owner columns + data assignment explícita, queries/updates owner-aware | Cross-account CRUD/read/branch/RAG/lab tests | 2, 5 |
| A2 Alto, bloquea activación | fallback `GET /{path:path}` sirve index HTML para `/api/...` no existente | Oculta path error y policy bypass misclasificado | Routing separado/catálogo | `/api/*` unknown JSON 404; UI allowlist | Test exacto API unknown no HTML 200, UI routes válidas | 1, 4 |
| A3 Alto, bloquea activación externa | Compose publica `8787:8787`; Dockerfile sin USER/TLS/proxy config | Exposición de HTTP/Root; cookies Secure no sirven sin HTTPS | Datos Portainer y operador | ingress cerrado o TLS proxy conocido, trusted proxy exacto, no-root y firewall | Probes desde redes + spoof Host/forwarded + cookie real | 7 |
| A4 Alto, bloquea integración del guard | 2B.1/2B.3 snapshots aislados; no lifespan/freeze/readiness conectado | route añadida evade inventario o app sirve con fallo | Registro final de módulos y policy registry | gate tras todos los registrations y antes de listen; drift no-ready | Late route/startup test en app real/módulos parametrizados | 1, 4 |
| A5 Alto, implementación de autorización | middleware ownership resolver separado de write; prototype explícitamente sin atomicity | TOCTOU IDOR, replay approval/interaction | refactor SQL/service | Predicados owner en SQL, transaction + CAS one-shot | concurrencia cruzada y múltiples request con exactamente un ganador | 1, 5, 6 |
| A6 Alto, activación | Pendientes actuales creados antes de identidad/consentimiento | replay/consentimiento anónimo tras bootstrap | política de cleanup/restore | invalidar approvals, expirar interactions/cancel runs pre-enable | fixtures pending se vuelven terminales; resume/approval denegados | 2, 5 |
| A7 Alto, activación Internet | Login rate limiting single admin susceptible a DoS; no MFA/monitor operativo | bloqueo de única cuenta; exposición brute-force | ingress profile / operator | LAN/VPN primero, calibrar KDF/lockout, alertas; MFA gate Internet | load/lockout/proxy tests y recovery runbook | 3, 7 |
| M1 Medio, implementación | prefs lazy y globales; no owner key | settings cross-user, migración no versionada | auth schema decision | migración preferences versionada a user key preservando valores | dos user prefs aisladas y legacy preserved | 2, 5 |
| M2 Medio, activación | No busy timeout explícito en app DB; SQLite serializa KDF/write | 503/latencia o lock contention bajo login | perf target hardware | medir workers/concurrencia; transaction corta y KDF fuera write lock si diseño seguro | contention/load/error fail-closed | 2, 7 |
| M3 Medio, operación | Secrets plaintext SQLite y backups; root puede acceder | robo de DB/backups = credenciales | filesystem/backup operators | ACL, cifrado/custodia, rotación incident response | restore drill, ACL, logs/redaction/secrets rotation | 3, 7, 8 |
| M4 Medio, streaming | 2B.3 es cooperativo, no aborta efecto ya iniciado; proxy SSE real desconocido | efecto en curso tras revoke; stall/buffering | cancelation/delegation design | límites de duración, abort signal, per-boundary checks; honest guarantees | revoke en cada await/effect y cliente disconnect/proxy tests | 6, 7 |
| B1 Bajo, integración/operación | Deprecation FastAPI `on_event`; health/liveness semantics actuales | futura lifecycle wiring frágil; diagnóstico confuso | lifecycle redesign | implementar gates en lifespan cuando checkpoint permita, separar health/readiness | startup order/failure readiness suite | 1, 7 |
| B2 Bajo, permisos policy | Catálogo final/roles y acceso AgentProfiles/MCP global no aprobados | policy inconsistent/privilege excessive | operador product decision | permisos específicos con least privilege y sin wildcard | policy review + permission truth table | 1 |

Los cinco C son críticos; C1/C2/C3/C5 impiden implementación segura completa y C4 impide operación/migración del despliegue. A3/A4 son bloqueos de activación incluso si la implementación pasa tests. No se compensa un bloqueo crítico con un proxy asumido.

## 10. Plan incremental recomendado

Secuencia refinada para mantener el scope y dependencias. Ningún checkpoint se ejecuta aquí.

| CP | Objetivo y archivos previstos | Cambios permitidos | Tests y GO/NO-GO | Parada, regresión y rollback |
|---|---|---|---|---|
| 1. Contratos y test harness | `tests/` y contrato/policy doc; posiblemente tests aislados existentes | Censo actualizado, route policy test, middleware ordering contracts; no activar rutas | Route matrix por módulo/framework/method, unknown API 404, body-read/side effect counters; GO sólo si catálogo exacto y policies aprobadas | Parar ante método/route no clasificado; rollback tests/docs sin runtime |
| 2. Persistencia/migración segura | `app/migrations.py` sólo en checkpoint futuro autorizado, tests migrations, nuevo store | Auth state/users/perms/session/guard/events, owners y preferences migration; no listener externo | DB fresca + legacy fixtures; SQL fault, kill/restart, ledger consistency, FK/integrity, backup restore, preservación | Parar con cualquier divergencia/owner huérfano; rollback restaurar backup, no downgrade destructivo |
| 3. Frontera admin host-side | `prototypes/` o nuevo `scripts/` explícito, tests/runbook | Adaptador host-side para NEXO DB, status/bootstrap/reset/revoke; nunca modificar CLI aislado para aceptar db | TTY/no argv secrets, UID/ACL/path/mount, lock web, backup, repeated bootstrap/recovery/fail-closed | Parar si path/UID operador no verificable; rollback binario host-side y restore, ingress cerrado |
| 4. Guard/routing completo | `app/main.py`, middleware/policy module, tests; fallback SPA | Envolver ASGI y conectar lifespan; todas policies, framework/mount/module; unknown API 404 | Anon/role/method matrix, route added late fails readiness, HTTPS/Host/Origin/CSRF, body unopened, zero side effects. **GO sólo estructural/integration-ready, no activation-ready** | Parar si ruta anónima/ambigua; CP4 no se despliega ni se expone; rollback sólo con ingress cerrado y conjunto compatible |
| 5. Ownership y autorización de dominio | repos `app/notebooks.py`, `app/agent_profiles.py`, handlers y nuevos repos/transactions/migrations/tests | owner SQL en conversations/notebooks/descendientes; preferences; global admin resources explícitos | IDOR para read/list/update/delete/branch/RAG/lab, joins legacy, concurrent owner changes, null owner denial. Requisito para hacer activable el guard de CP4 junto con CP6 | Parar con owner nulo/no resuelto; restaurar conjunto compatible y no permitir downgrade abierto |
| 6. Streaming/delegación/one-shot | `app/main.py`, `app/agent.py` / context/execution interfaces, approvals/interactions, tests | Pre-run auth, per-effect auth, delegated scopes, run cancel/revoke, state CAS | Fake providers/tools; auth before DB/run/task/stream, pre-header reauth, revocation before each effect, replay/expiry/cancel/resume. Requisito para hacer activable el guard de CP4 junto con CP5 | Parar ante side effect sin guard; revert sólo con ingress cerrado y código/schema/data/red compatibles; external effects no rollback garantizado |
| 7. Activación controlada | Dockerfile/Compose/proxy values/runbook sólo tras owner approval | no-root, secure ingress, readiness/liveness, recovery/backup + gates; all private routes same release | staging Portainer real, TLS/browser mobile, proxy spoof, restore+crash drill, full acceptance; signed GO | Parar en cualquier Unknown/GO faltante; cerrar ingress y restaurar backup validado |
| 8. Post-activation audit | `docs/` y pruebas operativas | revisión sin ampliar scope, monitoreo/retención, incidentes | route diff, denied/log redaction, access tests, backup restore, session revoke, owner samples | Suspender ingress ante anomalía; rollback operativo no implica auth off |

CP1 establece contratos antes de introducir controles; CP2 debe preceder uso real del auth store y CP3 permite el único canal de bootstrap; CP4 es integración estructural y no activable por sí sola. CP5 y CP6 son dependencias obligatorias para completar su protección de dominio/streams; CP4–6 deben estar completos y verificados antes de activation-ready. CP7 es decisión y operación separada. Los artefactos intermedios de CP2–CP6 sólo pueden usarse en entornos aislados con ingress cerrado; no desplegar versiones parcialmente protegidas. No arrancar Stage 15C por aprobar este plan.

## 11. Decisiones pendientes

- Instancia y scope de red: LAN, VPN, Internet; Portainer standalone/Swarm, proxy y HTTPS reales.
- Confirmación del operador para persistencia en SQLite principal, ownership retroactivo de conversaciones/notebooks, preferences y política de pendientes.
- Usuario único vs multiusuario y permisos finales; acceso a AgentProfiles, notebook links y diagnósticos; si recursos de usuario serán compartibles.
- Contrato migration runner: transacción por migración y estrategia sin `executescript` o migración offline; version/ledger validation y recovery tras crash.
- Operador host identity, mecanismo de doble control/break glass, ejecución host o contenedor, archivo path/ACL y cifrado/retención backups.
- scrypt parameters/TTL/lockout calibrados sobre hardware destino; DoS y MFA si se contempla Internet.
- Cookie origin/FQDN, trusted proxy exacto, CSP/XSS, rate/size/time limits, logs/cache de API/SSE.
- Delegación y revocación durante stream; límite cooperativo y aislamiento process/UID para herramientas mutativas.
- Versioning y policy de `/health`/readiness/docs, módulo `decision-runtime` y módulos futuros.

## 12. Recomendación final

**GO limitado** para assessment documental y preparación de los checkpoints de prueba. **NO-GO** para integrar auth guard/store en `app`, crear/aplicar migración, ejecutar bootstrap contra datos reales, modificar Docker/configuración, activar cookies, gestionar credenciales reales, desplegar o abrir ingress.

El repo demuestra API anónima, 75 bindings HTTP en la configuración de auditoría, fallback API amplio, runner con `executescript`, Compose HTTP publicado y sin `USER`; no demuestra estado de DB, backup, Portainer, TLS, firewall o proxy de una instalación. La condición para producción no es “health 200”: todos los críticos y bloqueos de activación deben cerrarse con pruebas/artefactos operativos y autorización expresa posterior.

## 13. Quality gates y alcance realizado

- `./scripts/nexo check`: **343 passed, 1 skipped**, exit 0. Cinco warnings Python ya observados (Starlette `BlockingPortal`, FastAPI `on_event` startup/shutdown y llamadas framework) y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`; sin fallos.
- `git diff --check`: OK. El documento untracked pasó también `git diff --no-index --check /dev/null docs/stage15b-3b-2b4-security-integration-readiness.md`.
- `git status --short --branch`: `## main...origin/main [ahead 12]` y únicamente `?? docs/stage15b-3b-2b4-security-integration-readiness.md`.
- HEAD, rama y stash se mantienen: `1fd58055dca1f9014b18d862ced024a0057b22f9`, `main`, hash stash `b7d2662a55bb2519aadd4291a93400c689afb46c`.

En esta etapa no se abrió ni copió SQLite productiva, no se modificó `app/`, `web/`, migraciones, Docker ni configuración, no se usaron credenciales reales, no hubo commit/push/deploy. Las pruebas de prototipo citadas en etapas previas no se interpretan como verificación de integración. Conteo final: 67 rutas de aplicación (65 core incl. SPA fallback + 2 módulo), 67 bindings de aplicación; 8 bindings framework (4 rutas GET/HEAD); **75 bindings método/ruta**, más mount `/assets` separado.
