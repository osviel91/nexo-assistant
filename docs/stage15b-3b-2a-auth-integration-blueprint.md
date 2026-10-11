# NEXO Stage 15B.3B-2A: Authentication Integration Blueprint

- Modalidad: auditoría y diseño; no implementación productiva
- Base esperada/verificada: `08f18cdea0928aefa89449c8b2e69e539451f0f4`
- Rama: `main`; `HEAD` coincide con la base
- Estado inicial: worktree limpio; `main...origin/main [ahead 8]`
- Stash histórico: `stash@{0}: On main: checkpoint: preserve pre-alignment worktree cce925b7`; preservado
- Software Docs: proyecto `Nexo Chat`, workspace `Osviel Workspace`, id `01a11dd0-e435-7ba2-a02a-3fbe2e27a54a`
- Decisión: **NO-GO para integración, activación o exposición**. GO sólo para revisar este blueprint.

## 1. Estado inicial verificado

El repositorio está en el commit exacto esperado. No se modificaron archivos durante el preflight. `git status --short --branch` mostró únicamente `## main...origin/main [ahead 8]`; no había diff staged ni unstaged. El stash indicado permanece intacto.

### Runtime y auth aislada

- `app/main.py` es la app FastAPI; construye SQLite en `NEXO_DATA_DIR/nexo.sqlite3`, registra rutas core y registra módulos al importar la app. En startup aplica `migrate()` y ejecuta lifecycle de módulos.
- En el runtime no hay autenticación general, middleware de sesión, política por endpoint ni `owner_user_id`. La API actual es anónima.
- `prototypes/admin_auth.py`, `prototypes/admin_auth_http.py` y `prototypes/admin_auth_cli.py` están aislados de `app/`. El core usa scrypt, admin único, sesiones opacas por hash, CSRF, rate limit persistente y eventos mínimos. El router sólo se monta en pruebas.
- El CLI requiere path explícito y TTY para mutaciones, valida permisos POSIX y rechaza `nexo.sqlite3`. **No es una herramienta autorizada para operar el SQLite de NEXO** ni está instalada como entrypoint del producto.
- El principal `service` del prototipo es un objeto lógico construible dentro del mismo proceso; no autentica un proceso ni constituye aislamiento de seguridad.

### Migraciones y despliegue

`app/migrations.py` registra 28 migraciones, con `_migration_28` como última en este HEAD. `schema_migrations.version` es una PK numérica y el runner aplica las migraciones pendientes en orden. Algunas migraciones usan `executescript`; por ello no se debe asumir que la cadena completa, tal como está, sea una unidad atómica ante cualquier fallo. La inspección de código **no determina** la versión ni el esquema de una instancia productiva.

El schema existente incluye conversaciones, mensajes/citas, runs/eventos/trazas, notebooks/fuentes/documentos/chunks, configuración/provider credentials, AgentProfiles, MCP, approvals e interacciones. `user_preferences` se crea perezosamente y no tiene versión en `MIGRATIONS`. Qdrant, si se usa, es índice denso derivado; la verdad canónica está en SQLite. No se abrió ni modificó base alguna. La topología real de proxy, HTTPS, Portainer, firewall, backup y volúmenes no fue inspeccionada.

### Arquitectura compartida

Se verificó el proyecto `Nexo Chat` en Software Docs. `architecture/overview.md` (Shared, revisión 2) cita el hash exacto esperado y separa runtime, prototipos, diseños futuros y desconocidos productivos. `architecture/security-administration.md` (Shared, revisión 1) describe el core aislado y recomienda SQLite principal para integración futura. `architecture/chat-request.seq` (Shared, revisión 2) describe el flujo implementado y valida con cero diagnósticos. Por tanto, el censo arquitectónico previo sí está incorporado en Shared. La lista también muestra una propuesta posterior de refresh abierta, pero no se modifica, aprueba ni promociona ninguna propuesta en este checkpoint.

No se crea una segunda copia documental en Software Docs: el blueprint local es el entregable canónico de esta tarea y duplicarlo no añade una vista causal o de ejecución distinta. No se creó contexto MY WORK ni propuesta Software Docs.

## 2. Arquitectura objetivo y límites

Objetivo: conservar `nexo.sqlite3` como estado canónico y añadir autenticación, autorización y ownership dentro de esa misma base. Sesión, ACL del recurso y transición one-shot de approval deben poder comprobarse en la misma transacción de dominio cuando la operación lo requiera.

Separar claramente tres estados:

| Capa | Estado observado | Lo que no implica |
|---|---|---|
| Runtime NEXO | FastAPI y SQLite operativos, sin auth general | No hay rutas privadas protegidas |
| Core aislado | Scrypt/session/CSRF/CLI probados con SQLite dedicada y fixtures | No es una migración, no usa la DB principal y no protege NEXO |
| Integración futura | Este contrato de destino, pendiente de decisiones y checkpoints | No autoriza despliegue ni significa que el proxy/producción sean seguros |

El servicio debe arrancar con acceso privado cerrado hasta que esquema, bootstrap, catálogo de rutas y política queden completos. No habrá flag productivo que convierta una migración fallida o auth incompleta en acceso anónimo. Durante la preparación, el ingress de una instalación existente permanece cerrado/restringido. El primer release que abra tráfico debe proteger todas las rutas privadas, no una selección por prefijos.

## 3. Persistencia propuesta

Las siguientes son tablas/columnas objetivo para diseñar y probar en un checkpoint de implementación futuro. No reservan número de migración ni constituyen SQL ejecutable.

| Estructura | Claves y restricciones | Uso e índices |
|---|---|---|
| `auth_state` | `id INTEGER PRIMARY KEY CHECK(id=1)`, `initialized CHECK IN (0,1)`, `policy_version`, `bootstrapped_at` | Singleton fail-closed; `initialized=1` sólo cuando usuario y ownership aprobado están confirmados |
| `auth_users` | `id TEXT PRIMARY KEY`, `username_normalized TEXT NOT NULL UNIQUE`, `password_hash TEXT NOT NULL`, `status CHECK(active,disabled)`, timestamps | MVP crea un admin local; hash versionado con KDF. No conservar contraseña ni tokens |
| `auth_user_permissions` | `(user_id, permission) PRIMARY KEY`, FK user `ON DELETE CASCADE` | Grants explícitos y allowlist; no wildcard ni permiso implícito `system:admin` |
| `auth_sessions` | `token_hash BLOB PRIMARY KEY CHECK(length=32)`, `user_id` FK, `csrf_hash BLOB CHECK(length=32)`, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at` | Índices `(user_id, revoked_at)`, `(revoked_at, idle_expires_at, absolute_expires_at)`; jamás guardar token opaco en claro |
| `auth_login_guard` | `subject TEXT PRIMARY KEY`, `failures CHECK(failures>=0)`, `window_started_at`, `locked_until` | MVP: bucket durable para el admin; no usar IP de forwarded header hasta validar proxy confiable |
| `auth_security_events` | `id INTEGER PRIMARY KEY`, `event_type` allowlisted, `actor_user_id` nullable FK, `created_at`, `correlation_id` nullable | Índice `(created_at,id)`; sólo evento/actor/tiempo/correlación, sin body, secretos, IP por defecto ni payload arbitrario |
| `conversations.owner_user_id` | FK a `auth_users`; nullable sólo durante transición controlada | Índice `(owner_user_id, updated_at DESC, id DESC)`; requisito antes de habilitar API privada |
| `notebooks.owner_user_id` | FK a `auth_users`; nullable sólo durante transición | Índice `(owner_user_id, updated_at)`; fuentes/documentos/chunks/citas heredan acceso vía notebook |
| `user_preferences` | evolucionar PK a `(user_id,key)` con FK | Asignar preferencias actuales al admin durante bootstrap; preservar valores |

No repetir `owner_user_id` sin necesidad en `messages`, `citations`, `runtime_runs`, `runtime_events`, `pending_approvals` o `runtime_interactions`: su autorización se deriva de la conversación padre, y la consulta debe unir/filtrar por propietario. Mantener FK existente con `ON DELETE CASCADE` donde ya aplica; valorar índices `runtime_runs(conversation_id,id)`, `pending_approvals(conversation_id,status)`, `runtime_interactions(conversation_id,status,expires_at)` sólo tras revisar planes y consultas reales. `conversation_tool_approvals` hereda del owner de conversación.

Providers, modelos, secrets de provider, AEMET/MCP credentials, AgentProfiles y configuración runtime siguen **administrativos/globales** en el MVP; no inventar usuario-propietario ni copiar claves. Los permisos de administración restringen su lectura y mutación. Cuando se habilite multiusuario se decidirá explícitamente qué recursos se comparten y quién puede administrar perfiles/tools.

### Revocación, limpieza y auditoría

- `revoked_at` en la sesión es la revocación durable; logout revoca una, reset/recovery revoca todas. La revocación debe quedar registrada en `auth_security_events` en la misma transacción.
- No borrar sesiones expiradas/revocadas antes de superar `absolute_expires_at` más una ventana operacional fijada en política. Luego un job por lotes elimina sólo filas de sesión; el evento de auditoría conserva su retención separada.
- Definir retención acotada de eventos antes de producción, con limpieza por lotes y sin borrar eventos de investigación sujetos a retención legal/operativa. No registrar cada intento durante un lockout activo.
- Un error SQLite, esquema desconocido, ausencia/inconsistencia de `auth_state` o busy timeout produce denegación (503 seguro) y no fallback. No incluir error crudo/SQL en respuesta o log.
- KDF scrypt actual usa `N=16384,r=8,p=1`; benchmark local previo no caracteriza hardware productivo. Mantener encoding versionado y calibrar coste/latencia y concurrencia en el hardware de destino. Transacción corta/serializada para estado de login; medir impacto de KDF bajo write-lock SQLite.

### Transacciones y backups

Bootstrap ejecutado fuera de banda, con app detenida e ingress restringido: `BEGIN IMMEDIATE`; crear admin, grants explícitos; asignar histórico sólo si existe aprobación del operador; invalidar pendientes; marcar estado completo; commit. Cualquier error hace rollback completo. El establecimiento de sesión/CSRF y contador de login es transaccional. Para aprobar tool: autenticar/revalidar sesión, permiso, owner y fingerprint; consumir aprobación por transición condicional `pending -> executing` en transacción; ejecutar fuera de transacción larga; persistir resultado y estado terminal con protección contra replay. La reanudación vuelve a validar antes de efectos.

Backup previo debe ser consistente: detener/quiescer writers y respaldar con SQLite Backup API, o copia offline verificada; incluir WAL coherente. Guardar cifrado y con ACL apropiada porque el DB incluye actualmente datos privados y credenciales provider/MCP/AEMET en texto. Registrar versión/hash y conteos, no contenidos sensibles. Si deployment usa Qdrant, capturar snapshot/identidad del índice en ventana compatible cuando la restauración requiera continuidad; auth no debe borrar ni reconstruirlo. Ensayar restore aislado de DB y artefactos con verificación de integridad y login antes de cualquier migración productiva.

## 4. Estrategia de migración e histórico

1. Antes de fijar una migración: revalidar HEAD y `MIGRATIONS`; inspeccionar, con autorización y sólo lectura, `schema_migrations`, `sqlite_master`, columnas, FK, integridad y features SQLite en una copia representativa de cada instalación. No asumir versión 28: el estado productivo es desconocido. No reservar aquí el número siguiente.
2. Mantener las migraciones aditivas. La nueva versión debe añadir tablas auth y columnas ownership nullable, índices/FK revisados y mecanismo de estado de migración. Probar DB fresca, cada forma legacy soportada y datos de producción anonimizados/sintéticos. No reescribir versiones publicadas ni inferir equivalencia por número de versión solamente.
3. SQLite por sí sola **no garantiza atomicidad del runner actual**: `migrate()` contiene migraciones con `executescript`, cuyo comportamiento de commit puede romper la expectativa de una única transacción envolvente. Antes de una migración productiva se exige un contrato probado de atomicidad del migrador y pruebas inyectadas de fallo/crash antes/durante/después de cada fase, reinicio/recuperación, repetición idempotente, coherencia entre objetos creados y `schema_migrations`, integridad/FK y conservación de datos. Si el runner común no puede ofrecerlo, encapsular/coordinar fuera del startup y no servir listener hasta terminar; no dejar app parcialmente disponible. Schema desconocido/futuro falla cerrado. No considerar suficiente una prueba feliz de una DB temporal ni el hecho de que SQLite soporte transacciones.
4. Separar cambio de esquema del claim del admin si hace falta, pero el proceso web no escucha en modo privado abierto. Detener servicio, restringir ingress, migrar sobre backup, ejecutar bootstrap host-side contra path verificado y asignación histórica atómica. Arranque web sólo si auth state está completo y cada conversación/notebook protegible tiene owner válido.
5. No intentar downgrade destructivo. Ante fallo: detener servicio, conservar DB migrada para diagnóstico y restaurar backup compatible completo en entorno cerrado. Revertir binario sin auth sólo con ingress aislado; nunca volver a exponer APIs antiguas anónimas.

### Asignación de datos

| Datos actuales | Tratamiento de activación | Razón / límite |
|---|---|---|
| Conversaciones y mensajes | Asignar conversación histórica al admin inicial tras aprobación explícita; mensajes heredan | Existe FK `messages.conversation_id`, no owner propio. Sin aprobación no completar bootstrap/abrir app |
| Runs, runtime events/traces, shadow observations | Heredan acceso por `conversation_id`/run padre; se asignan junto con conversación | No añadir propietario inventado a cada evento; validar endpoints de laboratorio por owner o permiso admin |
| Approvals pendientes | Marcar `invalidated` sin ejecutar en la transacción de activación; no heredar consentimiento anónimo | El snapshot/fingerprint protege integridad, no identidad/consentimiento del usuario |
| Interactions pendientes y runs `started/running/waiting_approval` | Expirar/cancelar de forma terminal y no reanudar tras activación. Registrar motivo no sensible | No existe identidad previa fiable; no replay por UUID |
| Notebooks, sources, documentos canónicos, spans, chunks, embeddings/citas | Preservar. Asignar notebook histórico al admin si operador autoriza; descendientes heredan | Índice vectorial no debe borrarse/reindexarse por auth |
| AgentProfiles y sus relaciones de tools/notebooks | Mantener globales y sólo administrables; revisar que un perfil no permita cruzar notebook no autorizado | No existe owner actual; no convertirlo en objeto personal sin decisión |
| Providers, modelos, keys, MCP/AEMET y configuración de embeddings/tools/runtime | Permanecen globales/de administración | Datos de sistema, no inventar ownership personal. Auth no cifra estos secretos |
| Preferencias `user_preferences` | Copiar cada par clave/valor existente a la fila del admin, preservando timestamp/valor; después particionar por user | Hoy tabla global creada perezosamente, sin `user_id` |
| Backups y Qdrant opcional | No migrar/eliminar; preservar snapshot coherente | Fuera de la base relacional de ownership |

Producir reporte de conteos por tabla y IDs/checksums si hace falta, nunca exportar prompts, documentos o secretos al log. Cualquier fila huérfana, owner ambiguo o estado no reconocido bloquea activación y requiere revisión, no asignación automática silenciosa.

## 5. Política HTTP y contratos

### Mecanismo central

Denegación por defecto. Cada `APIRoute` debe declarar exactamente una política: `public`, `user(permission)`, `admin(permission)` o `service(scope, audience)`. La identidad humana no satisface política de servicio. El permiso es una allowlist versionada; no usar wildcard. Ownership/ACL es una segunda comprobación obligatoria en la consulta/actualización del recurso, no una sustitución del permiso.

El inventario se congela **al final del startup/lifespan**, después de que el framework haya creado sus rutas y hayan terminado el registro de módulos y startup hooks, inmediatamente antes de que ASGI marque la aplicación disponible/acepte tráfico. El snapshot incluye cada método HTTP, WebSocket, mounts y rutas anidadas inspeccionables, rutas generadas por FastAPI (`/docs`, `/redoc`, `/openapi.json`, redirects OAuth) y handlers de framework. Un registro posterior invalida el fingerprint/snapshot y debe retirar readiness o fallar cerrado antes de servir la nueva ruta; el diseño productivo deberá impedir que el registro tardío deje la app disponible. Tests verifican cambio de snapshot ante ruta añadida después de congelación. No se infiere política pública de que una ruta sea de framework.

Cualquier ruta no anotada, método faltante o patrón ambiguo falla startup/readiness y test; la excepción no se interpreta como pública. Assets `/assets/*` y shell estático son excepción pública explícita; `/api/health` sólo liveness `{status}`. Readiness detallada sólo monitor privado o admin. Docs/OpenAPI se deshabilitan en producción o requieren permiso admin. Path `/api/*` desconocido nunca cae al frontend wildcard devolviendo index con 200.

Rutas pueden añadirse por `AttachmentsModule`, `DecisionRuntimeModule` o módulo futuro usando `include_router` o `app.post()`; su manifest/capabilities no define autorización. La ruta debe adjuntar política antes de completar registro. Guard ASGI previo al endpoint resuelve la ruta/método sin consumir body, autentica, valida Host/Origin/CSRF y rechaza antes de parsear multipart, leer archivos o disparar side effects. Después, dependencies/decoradores de dominio pueden realizar ownership-aware checks dentro de la operación SQLite. El middleware no debe mantener una tabla paralela silenciosa que pueda desincronizarse del endpoint.

El CLI aislado actual **no se modifica para aceptar directamente `nexo.sqlite3`**: su rechazo es una salvaguarda intencional, y cambiarlo no constituye una integración. El producto necesitará un adaptador/herramienta host-side específica para el schema principal, con autorización explícita del operador, path/volumen inequívoco, UID/ACL/TTY, backup verificado, preflight de versión e integridad, exclusión del proceso web y transacción/rollback comprobables. Hasta que exista y se revise, bootstrap/reset productivo no están disponibles; no crear endpoint remoto ni reutilizar el CLI aislado contra otra ruta por eludir la comprobación del basename.

### Matriz inicial

La matriz es propuesta para implementación y debe reconciliarse endpoint por endpoint con OpenAPI/inventario FastAPI final.

| Rutas actuales / objetivo | Política propuesta | Ownership/condiciones |
|---|---|---|
| `GET /api/health` | `public` reducido a liveness | Quitar versión/estado vectorial/errores; detalle privado |
| `/assets/*`, `GET /` y shell frontend estático | `public` explícito | Sólo assets no sensibles; UI pública no habilita datos/API |
| `POST /api/auth/login` | Público controlado, HTTPS + Host/Origin exacto, rate limit | No revelar si username existe; cookie no-store |
| `GET /api/auth/session`, `POST /api/auth/csrf`, `POST /api/auth/logout` | Sesión humana | CSRF y Origin para mutaciones; session/csrf no cacheables |
| `/api/settings/*`, `/api/providers*`, `/api/mcp/*`, `/api/agents*`, `/api/tools`, `/api/modules` | Admin: config/providers/mcp/tools/agents read/write/test/connect según operación | SSRF/egress policy independiente; claves nunca devueltas; pruebas salientes autorizadas antes de llamada |
| `/api/notebooks*` incluyendo contenido, sources, ingest, index y retrieve | Admin `rag:*` en MVP; permisos diferenciados read/content/write/query/diagnostics | Siempre ownership del notebook; contenido y retrieval no equivalen a metadata |
| `/api/conversations` GET/POST chat/PATCH/DELETE/branch | User `chat:read/write/delete` | Filtrar por owner; branch hereda owner; creación asigna caller dentro de transacción |
| `POST /api/chat` | User `chat:write`; CSRF requerido | Autorizar conversación existente o crear nueva para caller; validar profile/provider/notebook/tool eligibility antes de side effects |
| `POST /api/approvals/{id}` | User `tools:approve` | Owner por conversación; sesión vigente, one-shot, expiration, tool fingerprint/snapshot; 404 si ajeno cuando se evita enumeración |
| `POST /api/interactions/{id}` | User `agents:interact` | Owner por run/conversación; validar pending/expiry/schema; UUID no es credencial |
| `/api/lab/*`, benchmark y runtime diagnostics | Admin `diagnostics:read/run` | Filtrar datos por owner cuando ruta es de usuario; más simple MVP admin-only |
| `/api/files` (Attachments) | User `chat:write` | Guard antes de leer multipart; límites y CSRF |
| `POST /api/decisions` (módulo opcional) | Admin o servicio autenticado con scope según propósito confirmado | No público por ser opcional; declarar política aun si no se habilita |
| `/docs`, `/redoc`, `/openapi.json` | Deshabilitadas o admin-only | Deben estar en el inventario, no ignoradas como rutas framework |
| Cualquier ruta/API no clasificada | Fallo startup/test; `/api/*` desconocida se rechaza | Prohibido acceso permisivo de fallback |

401 = falta, sesión inválida, expirada o revocada. 403 = principal autenticado sin permiso, CSRF inválido o política denegada. 404 = recurso inexistente y, en rutas con riesgo de enumeración, recurso ajeno presentado de forma indistinguible. No reutilizar 404 para ocultar errores de autorización de configuración global sin política consistente. 400 para Host/Origin no aceptables según contrato aislado; 503 para auth store indisponible. Errores no incluyen SQL, existencia de cuenta ni contenido sensible.

### Sesión, CSRF y permisos

- Cookie `__Host-nexo_session`: opaque aleatorio 256 bits, sólo hash SHA-256 almacenado; `Secure; HttpOnly; Path=/; SameSite=Lax`, sin `Domain`; sólo HTTPS público. TTL de referencia del prototipo: 30 min idle/12 h absolute; decisión final y calibración pendientes.
- Contraseñas por TTY local one-shot, sin contraseña inicial, signup ni bootstrap HTTP; hash scrypt versionado con sal. Login uniforme y limitador persistente. Recovery local cambia hash y revoca todas las sesiones, sin afectar datos del dominio.
- CSRF sincronizador aleatorio, ligado a sesión y persistido como hash; mutaciones basadas en cookie requieren `X-CSRF-Token`, Origin exacto y HTTPS/Host configurado. `SameSite` no sustituye CSRF. Login/logout/rotación CSRF tienen contratos explícitos; no rotar CSRF mediante GET.
- Permisos nominales: `config:read/write`, `providers:read/write/test`, `secrets:metadata/write/rotate`, `agents:read/write/run/interact`, `tools:read/write/approve`, `mcp:read/write/connect`, `rag:read/read_content/write/query/diagnostics`, `chat:read/write/delete`, `diagnostics:read/run`. Grants al único admin se asignan explícitamente; decidir catálogo final y no crear `system:admin` implícito.
- Toda autorización precede a cambios DB, egress provider/MCP, cómputo benchmark/indexación, envío de archivo, creación de run, consumo de aprobación o apertura de stream.

## 6. Chat, SSE y AgentRuntime

El endpoint `POST /api/chat` actualmente valida/consulta y modifica DB antes de devolver `StreamingResponse`: inserta conversación nueva, mensaje y `runtime_run`, resuelve perfil/provider/notebook, y antes de que el cliente reciba eventos puede disparar hooks/shadow task. La autorización debe ocurrir al inicio, antes de ese bloque. No basta con autenticar dentro de `events()` o traducir una denegación a un evento SSE (eso entregaría HTTP 200 y quizá efectos ya iniciados).

| Paso | Comprobaciones antes de avanzar | Estado/efecto permitido |
|---|---|---|
| Crear conversación | Sesión user, `chat:write`, CSRF, provider/model habilitados, perfil/notebook accesibles | Insert conversación con `owner_user_id` caller, luego mensaje/run en transacción |
| Continuar existente | Sesión actual, `chat:write`, SELECT por `id AND owner_user_id`, sin run conflictivo | No usar existencia UUID como autorización; 404 si ajeno |
| Inicio generación/SSE | Autorización/CSRF/ownership completos, recursos validados, policy snapshot resuelto | Sólo entonces crear tareas/hooks/side effects y construir `StreamingResponse` |
| Crear run | Transacción con mensaje/owner; conservar actor humano y conversación | Guardar `initiating_user_id` y snapshot de política/recursos; no secretos ni token en metadata |
| Retrieval/RAG | `rag:query` además de la autorización de la operación iniciadora (`chat:write` en chat); ownership del notebook y provider permitido | Validar antes de embedding/HTTP saliente; citas se filtran por conversación propia |
| Herramientas | Scope delegado acotado, tool allowlist/fingerprint del snapshot y límite runtime | Modelo puede seleccionar entre tools ya autorizadas; no añadir permisos/scopes/owners |
| Solicitar approval | Verificar policy tool y consentimiento requerido | Persistir pending asociado a owner + run + conversación + fingerprint/args hash; no ejecutar |
| Resolver interaction | Sesión user, `agents:interact`, owner del run y estado/expiración/schema | CAS one-shot `pending -> approved/denied`; respuesta se entrega sólo al run autorizado |
| Resolver approval | Sesión actual, `tools:approve`, owner, pending/expiry, snapshot/fingerprint y CSRF | Consumir una vez. Revalidar antes de ejecución; no interpretar decisión previa a auth como válida |
| Reanudar tras approval | Reautenticar en cada request y validar sesión/owner/run/delegación vigente | Ejecutar sólo herramienta previamente aprobada; reanudar generador después del gate |
| Persistir resultados | Owner y run válidos; proteger transiciones concurrentes | Mensaje final, citations/artifacts y estado run dentro de operaciones existentes; no emitir a otro usuario |
| Reconectar/recuperar | Cada request nuevo valida sesión, owner, expiración y estado; no usar run/id como bearer | Si no hay protocolo de reconexión ya implementado, no inferirlo: devolver estado protegido o cancelar expirado |

Delegación futura: contexto inmutable emitido por backend con `actor_user_id`, `session_id` o revisión de sesión, `conversation_id`, `run_id`, `audience` exacta (`agent-runtime`/servicio), conjunto mínimo de `scopes`, provider y tool allowlist/fingerprint del snapshot, `issued_at`, expiración corta y `delegation_id` revocable. El runtime valida audience, expiración, revocación, run activo, ownership y scope antes de cada llamada con efecto. No acepta esos campos desde prompt/tool/model ni permite que el modelo cree/amplíe delegación.

Para AgentProfiles, `chat:write` autoriza iniciar/continuar la conversación y `rag:query` autoriza recuperar documentos; son controles acumulativos, no alternativos. La identidad de usuario necesita `rag:query` y ownership/ACL del notebook efectivo aunque un perfil lo referencie. Un AgentProfile sólo aporta configuración (provider/model, instrucciones, selección de herramientas/notebooks); no concede ni amplía grants del usuario. Resolver notebook explícito/conversación/perfil ocurre dentro del límite de notebooks visibles para el usuario. Si el profile referencia un notebook no accesible, rechazar antes de crear mensaje/run o hacer embedding, sin fallback implícito a un notebook diferente. Herramientas nativas/MCP que leen documentos reciben el mismo contexto delegado y verifican tanto scope `rag:query` como notebook/documento autorizado antes de leer; una tool allowlist por sí sola no da acceso a todos los notebooks.

Hay dos momentos distintos de autorización durante un run: (1) autorización inicial del request HTTP antes de persistir mensajes/run, ejecutar hooks, llamar provider o abrir `StreamingResponse`; (2) autorización renovada inmediatamente antes de cada efecto posterior (provider/RAG/tool, resolver approval/interaction, persistir una continuación). Una autorización inicial no es una concesión permanente para un stream largo.

Revocar una sesión impide requests nuevos y marca/revoca sus delegaciones derivadas. Revocar una delegación invalida futuros efectos aunque el run siga activo. Cancelar un run es una transición aparte y detiene iteración/efectos futuros de forma cooperativa; no equivale a revocar sesión ni revoca otras sesiones del usuario. En cada boundary de efecto se verifica sesión vigente o política explícita de continuation, delegación no revocada/no expirada, owner, run activo, audience/scope y snapshot. Una sesión revocada nunca se suple con una delegación obsoleta.

Terminal states (`completed`, `failed`, `cancelled`, `expired`, `revoked` u otro conjunto cerrado que se acuerde) se escriben mediante transición condicional/idempotente, sin reabrir run ni approval. Al detectar cancel/revoke, persistir el estado terminal y motivo allowlisted antes de cerrar el stream; resultados ya confirmados permanecen auditables, pero no se persisten nuevos outputs privilegiados. Los fallos DB al registrar la transición no permiten continuar efectos y deben dejar recuperación/reconciliación cerrada; no marcar éxito en memoria si no se confirmó persistencia.

Dentro del proceso Python actual esto es delegación lógica contra errores de programación, no autenticación real entre procesos. No emitir bearer/token interno autoafirmado y afirmar separación. Si el threat model requiere aislar compromiso de runtime, mover broker/tool executor a proceso/UID distinto con canal IPC autenticado y protocolo mínimo; es otro checkpoint. Sin esa frontera, el proceso web comprometido puede saltarse el wrapper y acceder a secretos SQLite.

## 7. Ownership, HTTP externo e infraestructura

- `owner_user_id` obligatorio para nuevas conversaciones/notebooks. Mutaciones y lecturas privadas deben aplicar owner en SQL/servicio, no después de cargar el objeto; joins de approval/run/interactions siguen la conversación.
- En bootstrap inicial, asignación de conversaciones/notebooks históricos al admin requiere aprobación explícita y reporte de conteos. Providers, modelos, profiles, credentials y settings permanecen globales de administración. Preferencias se migran como preferencias del primer admin.
- El principal administrador único no hace multiusuario seguro por sí mismo; las columnas y filtros ownership-aware establecen el contrato futuro y bloquean IDOR. ACL/compartición multiusuario queda fuera del MVP.
- Same-origin HTTPS detrás de proxy confiable o TLS directo confirmado. Proxy debe limitar Host, sobreescribir headers forwarded, no cachear `/api/*`/SSE, desactivar buffering SSE, fijar timeout razonable, limitar body/rate y no registrar cookies, CSRF, Authorization o cuerpos.
- Uvicorn sólo confía esquema/IP forwarded del proxy explícito y probado; nunca `*`. La app no toma decisión de HTTPS desde headers controlables. El origen exacto de Host/Origin debe ser configuración explícita.
- Preferir LAN filtrada/VPN; no asumir Portainer, Traefik/Nginx/Caddy, dominio ni TLS productivo desde Compose. Para acceso Internet: decisión independiente, MFA recomendado como gate, rate-limit/monitorización y CSP/XSS revisados.
- Auth no cifra SQLite ni sustituye filesystem ACL, backup cifrado, rotación de keys comprometidas o protección root/Portainer. El proceso actual es root según documentación/código de imagen previa; revisar UID/no-root como gate de despliegue, sin dar por hecho la instalación real.

## 8. Activación y rollback

Activación sólo mediante ventana de mantenimiento, binario con guard completo y migración ensayada:

1. Cerrar ingress externo, detener NEXO/writers y verificar identidad del host, path/volumen, versión de DB, esquema real, integridad y conteos sin mostrar datos.
2. Crear backup consistente cifrado y probar restore en entorno aislado. Conservar también artefactos/vector snapshot compatible donde corresponda.
3. Preparar proxy, FQDN, TLS, trusted proxy exacto, firewall/VPN y pruebas browser same-origin antes de abrir NEXO. No mandar credenciales por HTTP.
4. Aplicar migración aditiva revisada, comprobar ledger/constraints/foreign keys y reporte de conservación. Un fallo deja ingress cerrado; no iniciar versión vieja públicamente.
5. Ejecutar bootstrap/recovery local con TTY bajo identidad host autorizada; crear admin/grants, migrar owners aprobados, copiar preferencias, invalidar approvals/interactions/runs pendientes y completar `auth_state` atómicamente.
6. Arrancar versión que deniega startup si falta política de una ruta, auth state/owner no está completo, schema es incompatible o store no está disponible. Abrir sólo loopback/red privada de verificación.
7. Verificar login/logout, CSRF, 401/403/404/503, catálogo completo de rutas/módulos, ownership/IDOR, admin-only y acceso anónimo. Probar SSE antes/durante/after run, approval e interaction con proveedor/tool mock y sin credenciales reales.
8. Abrir acceso acordado sólo tras GO operacional firmado; mantener observabilidad y rollback preparado.

Rollback es restauración, no “deshacer código y seguir sirviendo”: detener ingress; restaurar binario compatible con el schema **sólo en red cerrada** o mantener versión auth; si esquema o datos incompatibles, restaurar backup coherente antes de reabrir. No borrar tablas/columnas auth ni reescribir ledger a mano. Si se decide volver a una app pre-auth, debe permanecer fuera de redes no confiables hasta completar downgrade/restauración validada. Migración de auth no modifica Qdrant; no restaurar vector snapshot de fecha distinta sin validar consistencia/citas.

## 9. Plan por checkpoints

Ningún checkpoint previo a activación es desplegable con ingress no confiable. Pasar unit tests no significa que el checkpoint sea seguro para producción.

| CP | Archivos/subsistemas | Dependencias y pruebas | Riesgo principal | GO / NO-GO y parada |
|---|---|---|---|---|
| A. Decisiones de operador | Sólo documentación/runbook; acceso, proxy, TLS, recovery, ownership histórico, KDF/TTL/MFA, backup | Confirmar topología y política; revisar lista explícita de rutas y datos | Diseñar sobre topología o propiedad supuestas | GO a preparación sólo con decisiones escritas. Parar ante DB/proxy/backup desconocidos |
| B. Core preparado para DB principal | `prototypes/admin_auth*` como base de diseño o nuevo módulo auth; no usar aún desde HTTP; schema/repositorio y CLI host-side | Tests unitarios/migración temporal, fail-closed, lockout/concurrencia, KDF hardware, path/UID/TTY, secrets/log redaction | Core aislado rechaza DB NEXO; error atomicidad/CLI sobre path incorrecto | GO técnico si pruebas fallan cerrado y backup/restore sintético pasa. Sigue NO-GO exposición |
| C. Catálogo/policy ejecutable | Registro de rutas FastAPI, módulos, health/assets/docs y pruebas de inventario | Enumerar todos los métodos/rutas tras registrar módulos habilitados y opcionales; route sin policy debe fallar; prueba body-read ordering | Ruta opcional o framework escapada; frontend fallback captura `/api/*` | GO sólo con cero rutas sin política en matriz completa. Cualquier excepción no documentada para |
| D. Migración/ownership | `app/migrations.py`, repositorios de conversaciones/notebooks/preferences/runs/approvals/interactions | Copias legacy representativas, DB fresca, FKs, rollback inyectado, conteos before/after, test IDOR | versión productiva desconocida, filas huérfanas, migración parcial | GO si backup restaurable y todas filas privadas resolubles; no abre listener. Parar en cualquier mismatch |
| E. Enforcement privado completo | middleware/guard central + todas las rutas core y módulos, auth routes | Matriz de permisos, 401/403/404/503, CSRF/Origin/Host, requests multipart antes de leer body, SSRF/side-effect deny | Despliegue parcial deja endpoints anónimos | Un release indivisible; sólo GO si inventario final íntegro y neg tests pasan. No habilitar login/guard parcial |
| F. Chat/SSE/AgentRuntime | `/api/chat`, approval, interaction, AgentRuntime/tool context, RAG, persistencia | Mocks provider/tool; conversión/create/continue; CSRF; autorización pre-run/pre-stream; replay, owner, expiry, cancel, resume/restart, resultados | Side effect antes de auth, replay approval, delegado con scope amplio | GO sólo si todo side effect ocurre tras revalidación y los streams fallan como HTTP antes de abrir. Sin reconexión inventada |
| G. Activación cerrada | build/release, auth bootstrap/recovery, proxy/HTTPS, backups, firewall/UID | Restore real en staging autorizado, prueba red/headers/cookie móvil, startup gates, rollback ensayo | Topología externa o backup no demostrado | Apertura controlada sólo con operador y gates firmados. Cualquier incertidumbre mantiene ingress cerrado |
| H. Revisión posterior/Stage 15C | Operación, seguridad, pentest | Auditoría de rutas, XSS/CSP, MFA si Internet, rotación secrets, revisión independiente | Confundir login implementado con seguridad integral | Decisión separada; Stage 15C no queda autorizada por este blueprint |

La preparación B–D puede avanzarse en entornos aislados; no puede desplegarse como “modo parcialmente autenticado”. El guard completo, ownership y endpoints administrativos deben llegar juntos antes de abrir tráfico.

## 10. Riesgos residuales

- No se conoce la DB, backup, proxy, TLS ni superficie de red productivos. No es posible declarar segura la activación en esa instancia.
- El login único es susceptible a denegación por lockout; calibrar rate limit e ingress privado y definir alertas sin confiar headers falsos.
- El proceso actual puede leer secretos plaintext de SQLite; auth web no defiende frente a RCE, root, propietario de Portainer o backup expuesto.
- XSS en UI puede operar usando sesión aunque cookie sea HttpOnly; revisar CSP/sanitización en gate aparte.
- SSE/proxy buffering, timeouts y cancelación del cliente pueden afectar estado de run; requiere pruebas integradas reales.
- Delegación dentro del mismo proceso es lógica, no frontera frente a código comprometido; tools mutativas pueden requerir proceso/UID aislado según amenaza.
- Writes SQLite serializados y KDF bajo contención pueden provocar 503; probar con worker count y hardware objetivo. No usar filesystem de red/multi-host como supuesto compatible.
- Añadir multiusuario exige definir ACL de AgentProfiles, tools, notebooks compartidos, secretos y auditoría; no se deriva automáticamente de `owner_user_id`.
- La invalidación de pendientes puede cortar trabajos legítimos, pero reusar consentimientos de una API antes anónima sería inseguro.

## 11. Recomendación

**NO-GO** para implementación de controles en rutas reales, migración de una instancia, recepción de credenciales reales, despliegue o apertura de tráfico. **GO limitado** para revisar este blueprint y preparar checkpoints aislados después de decisiones operativas y verificación autorizada del esquema/backup.

La recomendación de persistencia es SQLite principal por consistencia entre auth, ownership y acciones de dominio; tablas separadas por prefijo `auth_`, migración aditiva, backup cifrado y fallo cerrado. La base/versión productiva no está verificada. No existe ruta privada protegida hoy.

## 12. Verificación de esta tarea

Preflight de sólo lectura completado. Tras crear este documento se ejecutaron los comandos solicitados:

- `./scripts/nexo check` -> **290 passed, 1 skipped**, exit 0. Warnings: FastAPI `on_event` deprecated (startup/shutdown), Starlette `BlockingPortal` deprecated y Node `MODULE_TYPELESS_PACKAGE_JSON` para tres assets JS; sin fallos.
- `git diff --check` -> OK, sin salida. Como el nuevo documento no está trackeado, también se comprobó con `git diff --no-index --check /dev/null docs/stage15b-3b-2a-auth-integration-blueprint.md` -> OK, sin diagnósticos.
- `git status --short` -> `?? docs/stage15b-3b-2a-auth-integration-blueprint.md`; es el único cambio final.

No se abrió ni modificó SQLite, no se accedió a credenciales, no se alteró runtime, migraciones, Docker/Portainer o frontend. No hubo commit, push o despliegue. Stash preservado. Stage 15B.3B-2B y Stage 15C no iniciadas.
