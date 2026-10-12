# NEXO Stage 15B.3B CP2-B.2b-1: Ownership Schema Foundation

- Preflight inicial: detenido antes de implementar por incompatibilidad material de preferencias; desbloqueado por decisión arquitectónica explícita del operador.
- HEAD verificado: `995cab9ee8ca28dc415cb7a2d9e6b18fcea96686`, rama `main`.
- Worktree inicial limpio (`main...origin/main [ahead 17]`). Stash `b7d2662a55bb2519aadd4291a93400c689afb46c` presente e intacto.
- Entrega: cambios locales sin commit para revisión. Producción: **NO-GO**.
- La decisión aprobada conserva `user_preferences` global y añade `auth_user_preferences`; no se abrió SQLite productiva ni se ejecutó migración persistente.

## Resolución De Preferencias

El blueprint 2A propone evolucionar `user_preferences` a PK `(user_id,key)` y copiar las preferencias globales al primer administrador durante bootstrap. En el runtime actual, la tabla es opcional/perezosa: `app.main.ensure_preferences()` la crea con `key TEXT PRIMARY KEY`, sin columna `user_id` ni FK. `/api/preferences` lee/elimina por `key`; el PATCH escribe con `INSERT ... ON CONFLICT(key) DO UPDATE` (`app/main.py:690-726`).

Cambiar sólo el esquema a clave compuesta deja de proporcionar una restricción única sobre `key`; el `ON CONFLICT(key)` existente deja de ser válido para esa definición y los endpoints fallarían al escribir. La copia o asignación de valores legacy al administrador está expresamente prohibida en esta fase. En consecuencia, migrar la tabla conforme al blueprint rompe el comportamiento actual; conservarla intacta no prepara preferencias por usuario. No es posible cumplir simultáneamente el blueprint, la regla de no asignación y la compatibilidad runtime solicitada con una migración exclusivamente estructural sin decidir una arquitectura de transición.

El operador aprobó conservar `user_preferences` con su PK `key` y el upsert `ON CONFLICT(key)` sin modificar endpoints. Se implementa la tabla separada `auth_user_preferences(user_id,key,value,updated_at)`, con PK compuesta y FK `user_id -> auth_users(id) ON DELETE CASCADE`. Sus valores/lecturas permanecen sin uso en el runtime hasta una transición futura explícita. La tabla legacy no se crea mediante v30 si no existía, no se migra ni se copia y conserva las filas/tipos originales si ya existía.

Esto resuelve la incompatibilidad de DML identificada: el API actual sigue usando la PK legacy sin conocer la tabla nueva. La selección de preferencias legacy vs. usuario en la futura activación, el traspaso autorizado de valores y su compatibilidad/backfill quedan pendientes de CP2-B.2b-2. No se modifica Software Docs Shared.

## Inspección Del Modelo v29

Confirmado por `app/migrations.py` y el manifest v29 CP2-B.2a: ledger histórico 1–28 más migración transaccional v29; `conversations` y `notebooks` no tienen propietario antes de v30. V30 añade `owner_user_id TEXT REFERENCES auth_users(id) ON DELETE RESTRICT` nullable a ambas. La migración no ejecuta UPDATE ni crea usuarios: todos los registros históricos quedan NULL. Índices: `idx_conversations_owner_updated(owner_user_id,updated_at DESC,id DESC)` y `idx_notebooks_owner_updated(owner_user_id,updated_at)`. `ON DELETE RESTRICT` conserva la integridad del propietario si se introducen filas asignadas en fixtures/futuro.

| Dominio | Estructura observada | Tratamiento de ownership futuro a evaluar |
|---|---|---|
| Conversación | `conversations.id`; tiene `notebook_id` nullable sin FK; datos de mensaje referencian conversación | Owner directo en conversación; mensajes y ramas heredan por `conversation_id` |
| Notebook | `notebooks.id`; fuentes referencian notebook con cascade | Owner directo; no derivar propiedad de otros enlaces administrativos |
| Fuente/documento | `notebook_sources.notebook_id` FK cascade; `canonical_documents` referencia notebook y source; spans referencia documento | Herencia candidata desde notebook, pero validar joins y consistencia de los dos caminos notebook/source |
| Chunks/índice | `document_chunks` guarda notebook, document y source con FKs; FTS/vector son proyecciones | Sin owner duplicado; filtrar por notebook y verificar que las relaciones coinciden |
| Citas | FK a message; notebook/source/document/chunk son identificadores sin FK | Heredar conversación para autorización; integridad actual de IDs de documento no está garantizada por FK |
| Runs/eventos/traces | Runs apuntan a conversación; eventos apuntan a run; `shadow_observations` y `runtime_trace_events` guardan IDs sin FKs | Runs y eventos heredan por conversación/run; revisar traces sin integridad referencial antes de filtros |
| Interactions/approvals | Interactions y approvals conservan referencias a conversación y run; FK cascade; approvals también message FK | Heredar por conversación; futura autorización y CAS deben comprobar owner en consulta/transacción |
| Global admin | Providers, modelos, MCP, AEMET, embedding config, agent profiles/tools y settings sin owner | Permanecen globales/admin según blueprint; no añadir owner |
| Preferencias | Tabla legacy ausente hasta acceso HTTP; cuando existe, PK global `key`. V30 añade `auth_user_preferences(user_id,key,value,updated_at)` con PK `(user_id,key)`, FK cascade, `key` no vacía, `updated_at TEXT NOT NULL` y `value TEXT` nullable | Aislamiento estructural por usuario; no hay lecturas/escrituras runtime ni traslado de valores legacy |

## Superficie Runtime Revisada

- Conversaciones: list/get/delete/patch/branch en `app/main.py:1289-1529`; chat crea/lee/escribe conversación y mensajes en `app/main.py:1539+`; approvals y continuaciones modifican conversación/run fuera de esas rutas iniciales.
- Notebooks y descendientes: `/api/notebooks*`, fuentes, documentos, indexación y retrieval en `app/main.py:902-1086`; persistencia en `app/notebooks.py`; retrieval/index en `app/retrieval.py` y `app/vector_index.py`.
- Preferencias: GET/PATCH `/api/preferences` en `app/main.py:708-726`.
- Interactions: POST `/api/interactions/{interaction_id}` en `app/main.py:501`; approvals: POST `/api/approvals/{approval_id}` en `app/main.py:1327`.
- Attachments independientes: módulo `app/modules/attachments.py`; vinculación ulterior a mensaje/conversación debe revisarse antes de atribuir ownership.

Esta inspección sólo clasifica rutas y relaciones para una fase posterior; no alteró sus consultas ni permisos. El runtime sigue sin enforcement de ownership.

## Estado De Verificación

## Implementación Y Recuperación

V30 se registra en `TRANSACTIONAL_MIGRATIONS` con statements individuales: dos `ALTER TABLE`, dos índices y la tabla de preferencias por usuario. DDL y ledger se confirman en el mismo `BEGIN IMMEDIATE`; manifest v30 incorpora columnas/FKs/índices exactos y sigue admitiendo la tabla legacy `user_preferences` sólo como extensión opcional. Runtime reconoce los manifests v28, v29 y v30; sólo v30 incluye ownership estructural. La presencia de `owner_user_id` o un valor NULL no concede autorización: no se alteraron consultas, handlers, rutas, middleware o permisos efectivos.

Pruebas focales: migración v29→v30; compatibilidad desde v28/v29; preservación de filas sintéticas de conversaciones, notebooks, mensajes, runs/eventos, sources, documentos/spans/chunks y preferencias legacy; owners históricos NULL; inserción de owner válido; rechazo de FK inexistente y borrado de propietario referenciado por cada tabla; aislamiento/duplicados de `auth_user_preferences`; GET/PATCH legacy de `/api/preferences`; manifest con mutaciones de columna, FK `ON DELETE`, índice y constraint, objetos ausentes y ledger futuro; SQL fault rollback; SIGKILL pre/post commit y antes/después de replace; concurrencia de migradores; backup/restore/reapertura.

SIGKILL transaccional: tras `after_ledger`, el staging reabre como v29, sin columnas owner v30; tras `after_commit`, reabre como v30 íntegro, con ledger 1–30, owner NULL, relaciones y preferencias legacy preservadas y tabla user prefs vacía. En ambos casos la canónica v29 queda intacta y el retry promueve v30. SIGKILL antes y después de `os.replace` también reabre bajo journal de recuperación; el retry deja v30 íntegro con ledger 1–30, nueve tablas de recursos dependientes con la fila sintética intacta, preferencias legacy preservadas y ownership NULL. La suite CP2-B.1 valida además la generación v30, `integrity_check` y FKs. La prueba concurrente muestra que el segundo migrador no obtiene el lease mientras el primero conserva el EX; terminado el primero, v30 valida. No se simula pérdida eléctrica, filesystem productivo ni SQLite productiva.

Resultados finales: focal CP2-B.2b-1 + CP2-B.2a + CP2-B.1 + CP1: **55 passed** (5 warnings conocidos de Starlette/FastAPI). `./scripts/nexo check`: **420 passed, 1 skipped**, exit 0 (5 warnings Python conocidos y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`). `git diff --check` y whitespace de archivos nuevos: sin diagnósticos.

## CP1, Transición Y Límites

No se cambia `MIGRATION_EVIDENCE`: esta prueba demuestra migración/schema de v30 sobre SQLite temporal, pero no bootstrap, protección HTTP, ownership-aware queries ni activación. Ownership permanece contrato/prototype-level; nada es `operational-verified`. Los endpoints siguen anónimos y sin filtros por owner. NULL es estado histórico/transitorio no autorizado, no acceso público ni pertenencia implícita al administrador.

CP2-B.2b-2 debe definir y ejecutar separadamente, con consentimiento explícito, identidad del admin inicial, transacción de asignación única/rechazo de repetición, inventario y conteos pre/post, tratamiento manual de huérfanos, invalidación de approvals/interactions pendientes, revisión/cancelación de runs activos, auditoría, backup verificado y rollback/restauración. La verificación debe probar cero owners NULL donde se requiera, FKs limpias y consultas ownership-aware antes de activar acceso. No asignar preferencias históricas automáticamente. Esta fase no implementa ese proceso, bootstrap, auth, filtros ni cambios de rutas.

Condiciones aún pendientes: transición de preferencias legacy/por-usuario, bootstrap autorizado, ownership histórico, CP4 guard completo, CP5/CP6 consultas y efectos ownership-aware, recuperación host-side, backup/restore operacional y evidencia de proxy/TLS/ingress. No iniciar CP2-B.2b-2, CP3 ni Stage 15C.
