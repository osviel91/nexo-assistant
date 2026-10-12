# NEXO Stage 15B.3B CP2-A: SQLite Migration Safety

- Modalidad: auditoría, harness experimental y propuesta para CP2-B; sin cambios productivos
- HEAD de preflight: `c04a8632a3c8c846f13a37b614af4c08db63f3c9`, rama `main`
- Estado inicial: worktree limpio; stash histórico `b7d2662a55bb2519aadd4291a93400c689afb46c` intacto
- Resultado: **GO limitado para CP2-A; NO-GO para migrar, bootstrap, activar o exponer producción**

## 1. Alcance Y Seguridad

El trabajo usa SQLite temporal dentro de `TemporaryDirectory`, incluyendo sidecars WAL/SHM, e importa `app.migrations` y `app.main` sólo en fixtures/procesos aislados. En las pruebas de crash, procesos hijo reciben rutas temporales explícitas y el padre los sincroniza por una línea `READY` en stdout antes de enviar SIGKILL. No hay sleeps de sincronización. La base `app.main` se importa con `NEXO_DATA_DIR` temporal; no se ejecuta startup ni se contactan servicios.

No se leyó, copió ni abrió SQLite productiva; no hubo credenciales reales, providers, Qdrant, Arbiter, MCP externo, contenedor ni servicio host. No se tocaron `app/`, `web/`, `app/migrations.py`, migraciones 1–28, Docker, Compose, Portainer o configuración. No se modificaron los prototipos existentes de auth/persistencia.

El prototipo nuevo `prototypes/sqlite_migration_safety.py` es laboratorio, no runner instalable. Reutiliza `backup_database()`/`restore_database()` de 2B.2 para los experimentos de backup, sin relajar su confinamiento a archivos temporales.

## 2. Migrador Actual Auditado

`app.migrations.MIGRATIONS` contiene versiones consecutivas 1–28. Las funciones se acumulan en el orden de definición y el runner lee ese tuple, pero la versión 28 no representa por sí sola un fingerprint del esquema. Quince migraciones invocan `executescript`: **1, 2, 3, 5, 6, 8, 9, 10, 12, 18, 19, 22, 23, 26 y 27**. Se mezclan `executescript`, `execute`, ALTER TABLE, índices, triggers, FTS5 y backfills DML; la migration 12 combina creación/backfill de FTS con ALTER/UPDATE posterior.

Contrato observado de `migrate(connection)`:

1. Ejecuta `CREATE TABLE IF NOT EXISTS schema_migrations` antes de validar el ledger.
2. Lee las versiones a un `set` una sola vez. No exige continuidad, no rechaza versiones futuras y no contrasta objetos, columnas, constraints, FK, FTS, checksums o estado parcial.
3. Recorre las funciones conocidas en orden; si un número no estaba en el set, ejecuta el callback y sólo después inserta `(version, datetime('now'))`.
4. No inicia `BEGIN`, no aplica rollback propio, no fija journal mode, no establece un bloqueo de migrador ni captura errores. La transacción final depende del caller/conexión; no se asume que el context manager arregle commits hechos por `executescript`.
5. La `PRIMARY KEY` del ledger evita duplicar el mismo número, pero no serializa la decisión inicial de dos procesos que ambos observaron la migración como pendiente.

`app.main.db()` llama `sqlite3.connect(DB_PATH)` con defaults del entorno, asigna `row_factory` y activa `PRAGMA foreign_keys=ON`; no fija journal mode ni busy timeout. Observación aislada en este host: SQLite **3.49.2**, `journal_mode=delete`, `foreign_keys=1`, `busy_timeout=5000 ms`. El busy timeout medido proviene del default del driver, no de política explícita de NEXO.

La referencia del driver es la documentación de [`sqlite3`](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Cursor.executescript): con el control transaccional legacy usado por defecto, `executescript()` confirma una transacción pendiente antes de ejecutar el script. `Connection.backup()` es la API utilizada en los ensayos de snapshot; los resultados reportados se verificaron además experimentalmente en este entorno.

`startup()` llama `migrate()` dentro de `with db()`, antes de `module_registry.startup()`. Además `embedding_configuration()` y `knowledge_health()` llaman a `migrate()` desde handlers. El context manager de `sqlite3.Connection` hace commit/rollback de una transacción pendiente, pero no cierra la conexión y no constituye por sí solo un límite por migración, bloqueo global ni protección frente a commits implícitos de `executescript()`.

No se puede concluir que todas las bases reales estén en v28, tengan el esquema del repo, el mismo journal mode, SQLite/FTS5 compatibles, sidecars quiescentes o ausencia de escritores externos. Ninguno de esos estados productivos se inspeccionó.

### Migraciones históricas 1–28

No reescribirlas ni envolverlas retrospectivamente en una transacción externa suponiendo atomicidad. Para un origen anterior a v28, CP2-B deberá conservar el backup verificado como original inmutable, aplicar la cadena histórica exacta sobre un destino de staging descartable con writers detenidos y sin listener, y tratar cualquier interrupción/error como destino staging contaminado: desecharlo y recrearlo desde el backup, no reintentar sobre el estado parcial. No envolver externamente los callbacks históricos que llaman a `executescript()` esperando rollback total ni reescribir su contenido/número retrospectivamente. Validar versión, manifest estructural y datos antes de publicar/switch del destino. La política concreta de sustitución/switchover del archivo en el volumen deberá verificarse en su filesystem y con la aplicación detenida; CP2-A no demuestra un rename/publicación operacional.

Para un origen ya validado en v28, la migración administrativa nueva puede usar la unidad transaccional candidata descrita abajo. Una base histórica que no coincida con alguno de los estados admitidos no se auto-repara: queda cerrada para auditoría/restauración. Esto evita modificar migraciones publicadas y no convierte la cadena histórica en atómica; confina su posible estado parcial al staging que nunca se publica.

## 3. Experimentos Y Resultados

Comando focal:

```sh
./.venv/bin/python -m pytest -q tests/test_cp2a_sqlite_migration_safety.py
```

Las pruebas invocan el `migrate()` real en fixtures. Para introducir fallos deterministas, algunas sustituyen temporalmente `migrations.MIGRATIONS` con una única callback sintética, sin cambiar el módulo productivo ni sus migraciones. Los tests históricos exitosos usan las funciones 1–28 reales.

| Experimento | Procedimiento aislado | Resultado observado |
|---|---|---|
| Histórico real | Construir por funciones reales hasta v1, v12, v21, v27 o v28; insertar dato sintético; continuar con el runner hasta v28 | Ledger continuo e integridad/FK válidos; fila sintética preservada en todos los puntos de partida probados |
| SQL inválido dentro de `executescript` | Runner real con callback que crea tablas, inserta una fila y falla en la sentencia siguiente | `executescript` deja objetos y fila previos; el ledger no registra v1. `integrity_check=ok` no detecta que el estado sea semánticamente incompleto |
| `executescript` con transacción exterior | `BEGIN IMMEDIATE`, DDL+DML previo; script con otro DDL+DML; rollback y reapertura | `executescript` confirma la transacción exterior antes del script; ambas tablas/filas siguen tras rollback/reapertura. Un wrapper exterior no hace atómica la secuencia |
| Excepción después de DDL+DML `execute` | DDL antes de DML y excepción dentro de `with Connection`; salir propagando la excepción | La tabla queda, el DML pendiente se revierte y no aparece versión. Muestra estado parcial aun usando el context manager |
| Reintento actual | Reejecutar fixture parcial usando `IF NOT EXISTS`/`INSERT OR IGNORE` | El fixture llega a ledger v1; es recuperación de esa operación deliberadamente idempotente, no prueba que cualquier migración real sea reejecutable |
| Gaps/futuro/objeto extra | DB v28 sintética con v12 borrada, v29 añadida y tabla inesperada | El runner vuelve a ejecutar v12, conserva v29 y la tabla inesperada, y retorna sin validar ni rechazar versión/esquema desconocidos |
| Ledger v28 con objeto requerido ausente | Eliminar `pending_approvals` y volver a llamar al runner | Retorna sin recrear ni detectar el objeto faltante; el número de versión no valida el esquema |
| Carrera de dos runners actuales | Barrier en callback después de que ambas conexiones leen el ledger pendiente | Ambas entran en el camino de ejecución; una completa y la otra falla en almacenamiento/ledger. No hay exclusión `BEGIN IMMEDIATE` a nivel de migración |
| Candidate, fallos en fronteras | Fault hooks antes, después de statements, antes del ledger y después de insertar ledger pero antes de commit | Con `BEGIN IMMEDIATE`, schema/data/ledger vuelven al estado previo; retry completo funciona y repetir versión aplicada valida y no duplica datos |
| Candidate, error SQL | La tercera sentencia inserta en una tabla inexistente después de DDL/DML válidos | Excepción SQL revierte toda la transacción, incluido el ledger inicial de v1 |
| Candidate, schema parcial/ledger inválido | Esquema previo incompatible, ledger con gap/futuro, tipo/PK del ledger incorrectos, `foreign_keys=OFF` y FK huérfana | Se rechazan antes de aplicar; objeto parcial preexistente no se repara ni se acredita |
| Manifest estructural | Fixtures con PK, `NOT NULL`, defaults/columnas, `CHECK`, `UNIQUE`, FK, índice explícito, trigger y view declarada como extensión | El estado exacto valida; constraint alterada, extensión alterada o un objeto no listado se rechaza |
| Manifest integrado al candidato | Validadores de entrada/salida code-owned; migración que añade un índice | DDL+ledger se aplican con el manifest esperado; quitar luego el índice hace fallar incluso el no-op de versión ya aplicada |
| Bootstrap simulado | Tablas `cp2a_fixture_*` solamente; error entre identidad sintética y estado `initialized` | Rollback; permanece `initialized=0`; no se crean tablas `auth_*` |

`with sqlite3.Connection` no fue equivalente a rollback de los scripts. En SQLite del entorno, DDL/DML confirmados por `executescript` sobrevivieron la falla posterior; en el caso `execute`, el límite de transacción heredado del context manager revirtió el DML pendiente, pero no la tabla previamente creada. La garantía depende de qué statements abrieron/cerraron transacciones, no del `with` aislado.

## 4. Terminación Abrupta

En POSIX, pruebas padre/hijo separadas sincronizan el instante por stdout y terminan al hijo con `SIGKILL` real. Verifican status de salida `-SIGKILL`, reabren el archivo, consultan ledger/esquema, `integrity_check` y `foreign_key_check`, y luego ensayan el reinicio:

| Caso | Journal | Punto de terminación | Estado después de reabrir |
|---|---|---|---|
| Runner actual, callback de fixture con `executescript` | DELETE y WAL | Script ya creó objeto/dato; hijo detenido antes de que el runner inserte ledger | Objeto y fila confirmados, ledger sin versión. Integridad física OK. Retry sólo termina para el fixture idempotente |
| Candidate explícito | DELETE y WAL | Después de primer statement o después de insertar ledger, aún antes de commit | Padre reabre DB: SQLite recupera rollback, no hay tabla ni ledger de v1; reinicio aplica v1 completa |
| Candidate explícito | DELETE y WAL | Proceso termina después del commit confirmado | Padre reabre DB y encuentra tabla, dato y versión 1 completos; la versión aplicada valida schema y hace no-op |

Esto verifica terminación de proceso y recuperación local SQLite. No es un reinicio de contenedor, no mata un host y **no demuestra pérdida de energía, filesystem/storage failure, NAS, hardware write cache ni `fsync` real**. Tampoco mata el proceso dentro de una de las sentencias internas de las migraciones publicadas: el fixture actual se detiene tras completar `executescript` y antes del insert del ledger. Esa diferencia queda explícita.

## 5. Estrategia Candidata

El harness candidato usa una unidad por versión y por conexión en reposo:

1. Requerir `foreign_keys=ON` antes de abrir la transacción; `BEGIN IMMEDIATE` obtiene el writer lock antes de validar estado previo.
2. Validar forma y secuencia exacta del ledger, rechazando gaps/futuro/versión fuera de orden. Para migración ya aplicada, comprobar el schema post y no volver a ejecutar el body.
3. Validar explícitamente el schema de entrada, no inferirlo desde el ledger.
4. Ejecutar cada sentencia individual con `execute()`; no dividir scripts por `;` y no llamar `executescript()` dentro de esta frontera. Un futuro migration debe declarar statements/rewrite de datos de forma compatible con el validador.
5. Validar schema de salida, `integrity_check` y `foreign_key_check`; insertar la versión sólo después de todas las comprobaciones.
6. Commit de statements + DDL + fila de ledger en la misma transacción; cualquier error llama rollback y no avanza readiness. El bloqueo/busy timeout debe ser explícito y un lock timeout falla cerrado.

El harness rechaza ledger futuro/gap, ledger con tipo/PK inadecuado, foreign keys apagadas, FK huérfana, DDL inesperado en el estado inicial y schema no coincidente. Dos migradores candidatos concurrentes se serializan: uno aplica y el segundo observa la versión completada y hace no-op validado. Esto **no se integra** en el runner real. El callback de schema anterior/posterior representa una manifestación futura, que en CP2-B deberá ser código auditable con objetos/columnas, índices, triggers, FK, virtual tables y invariantes de datos esperados por versión; no basta con reusar el callback permissivo de una fixture.

### Contrato estructural del candidato

`SchemaManifest`/`validate_schema_manifest()` en el prototipo comprueban un snapshot estructural esperado, no sólo `MAX(version)`:

- Objetos SQLite: conjunto exacto de tablas, índices, views y triggers de usuario, con tipo, nombre, tabla asociada y SQL almacenado. El SQL de `CREATE TABLE` se compara literalmente para conservar constraints `CHECK`, `UNIQUE`, PK y FK; un cambio de constraint se considera drift incompatible.
- Columnas de cada tabla: nombre, tipo declarado, `NOT NULL`, default, ordinal de primary key y flag hidden/generated mediante `PRAGMA table_xinfo`.
- Índices: nombre, unicidad, origen, partial y todas las columnas/expresiones/flags de `PRAGMA index_list` + `index_xinfo`; el SQL del índice explícito también forma parte del manifest.
- Claves foráneas: todas las filas de `PRAGMA foreign_key_list`, incluidos tabla/columnas, orden y acciones `ON UPDATE`/`ON DELETE`/`MATCH`.
- Ledger: contrato independiente del manifest, con columnas/version type, PK de `version`, `applied_at NOT NULL`, secuencia exacta sin gap ni futuro y fila escrita en la transacción de schema.
- Objetos adicionales: prohibidos por defecto. Una extensión legítima debe aparecer como objeto adicional permitido en el manifest versionado, con definición SQL y metadata estructural exactas; permitir sólo un nombre/prefijo no basta. Si su DDL/columnas/index/FK derivan del esperado, falla como drift.

La prueba compara manifests generados desde fixtures controladas: confirma columnas normales y generadas, index/constraint/FK, permite una view adicional exacta cuando está declarada y rechaza tabla no listada, cambio de constraint y cambio de definición de esa extensión permitida. El helper de captura se usa sólo con esquemas de fixture/build confiables: **no se debe capturar el manifest desde la DB que se intenta validar y luego confiarlo**, pues eso bendeciría el drift. CP2-B debe versionar manifests de entrada/salida en código/release y decidir explícitamente extensiones soportadas. Se excluyen objetos internos `sqlite_*`; virtual tables y sus shadow tables de usuario sí quedan inventariados.

`Connection.execute()` sólo admite una sentencia por llamada. No se debe inventar un parser por `;` porque SQL permite delimitadores en strings, comentarios y cuerpos de trigger. Transformar explícitamente cada migración futura en statements revisables o definir un script completo con transacción interna y manejo de fallo probado, sin mezclarlo con una transacción externa que `executescript()` pueda confirmar implícitamente. Recomendación CP2-B: no permitir `executescript` en la ruta transaccional nueva.

## 6. Esquemas Y Estados Parciales

Las fixtures v1/v12/v21/v27/v28 se generan llamando las funciones reales del repo en orden, y se prueban desde ahí hasta v28. FTS5 está disponible en la SQLite observada. Además se construyen fixtures sintéticas explícitas para ledger gap/futuro, objeto faltante/inesperado, estado parcial, DDL de esquema previo incorrecto y FK desactivada/huérfana.

El runner actual:

- Reintenta versiones faltantes aunque haya una versión mayor ya registrada; no rechaza el futuro.
- Puede retornar con ledger completo y una tabla de dominio requerida eliminada.
- No detecta objetos inesperados.
- No compara una firma de esquema contra el release del código.
- Deja decisiones de reparación al carácter incidental de cada migración (`IF NOT EXISTS`, `_add_column_if_missing`, backfill condicional). No hay evidencia para auto-reparar de forma segura una base productiva parcial/desconocida.

El candidato sólo recupera automáticamente un crash en el que la transacción completa fue revertida y el estado previo sigue validado. Si la base arranca con estado parcial heredado, el candidato exige que el validador de entrada lo reconozca; si no, rechaza y deja el original sin cambios. No recomienda “reconciliar” objetos ni completar ledger a mano.

## 7. Backup Y Restore

Se reutiliza SQLite Backup API a destino nuevo dentro del temp root. La prueba abre un NEXO v28 sintético con provider/datos de fixture y payload suficiente, mantiene un writer WAL abierto con `wal_autocheckpoint=0`, confirma sidecar `-wal` presente y provoca otra escritura concurrente mediante callback de progreso + `Future`, sin sleep. El snapshot contiene una sola generación íntegra (la anterior o la nueva), incluye el dato confirmado en WAL, mantiene versiones 1–28, schema, conteos, integrity y FK; el original queda en la generación nueva. Restore a un archivo nuevo preserva ledger/schema/datos.

Una falla inyectada durante copia no publica destino parcial ni cambia el archivo fuente; retry al mismo destino ausente funciona. El helper 2B.2 ya cubre fallos de backup/restore, WAL writer controlado, destino existente y validación estructural del archivo temporal; CP2-A no modifica ese prototipo.

La prueba de backup genérico sólo prueba integridad física y FK. Un backup/restore de v27 puede ser legible e íntegro y aun así ser incompatible con código que exige v28 (`mcp_tools.read_only_hint`). El test restaura ese archivo y su schema gate lo rechaza. Restaurar SQLite no restaura Qdrant ni garantiza consistencia de índices externos; éstos necesitan snapshot/versionado independiente y plan de reconciliación.

No copiar el `.sqlite3` activo con writers. La recomendación operativa es detener/quiescer writers o usar Backup API coordinada, verificar manifiesto/ledger/schema/data en staging y sólo publicar/switch del destino tras validación; preservar el original y mantener ingress cerrado.

## 8. Integración Con CP1

`prototypes/security_contracts.py` y su prueba CP1 incluyen siete gates nuevos: migración incompleta falla cerrada; ledger y schema se confirman juntos; schema validado más allá de número; versión futura desconocida rechazada; error de almacenamiento no habilita fallback anónimo; restore incompatible no habilita servicio; bootstrap exige schema y backup verificados.

El gate de activación requiere **`runtime-verified` literal por requisito**. Los experimentos de esta fase sólo prueban harness/runner aislado, por tanto su evidencia es `prototype-verified`; la prueba comprueba que esos estados mantienen todos los migration blockers. Ningún resultado de CP2-A cambia el estado a runtime u operational ni activa readiness. El runtime NEXO sigue sin guard/persistencia integrada.

Para CP2-B: autorización de etapa explícita; bloquear listener/ingress; backup representativo autorizado; manifest de esquemas históricos soportados; migración offline con unit-of-work por versión; fallo de cada SQL boundary; SIGKILL pre/post commit en DELETE y WAL; contención/concurrencia; startup cerrado con ledger/schema mismatch; restore validado; prueba de bootstrap transaccional en fixture. No declarar `migrations_verified` hasta conectarlo al código real y a una integración representativa autorizada.

## 9. Límites, Riesgos Y Decisión

- Los fallos de migration 1–28 que contienen `executescript` no se inyectaron uno por uno en cada sentencia; la evidencia actual combina auditoría fuente, ejecución íntegra real de históricos y reproducción experimental usando el runner real con callback de fixture.
- SIGKILL no valida corte de energía, reinicio de contenedor, almacenamiento de red ni recuperación de filesystem.
- Las fixtures históricas provienen del repo, no de instancias instaladas; productivo podría ser pre-v28, alterado o tener objetos con SQL distinto.
- La fixture partial con `IF NOT EXISTS` muestra que retry puede funcionar para un callback concreto, no una garantía global de idempotencia.
- Revalidación de schema exige manifest específico por release; ledger no autentica archivo ni demuestra que los objetos correspondan a ese release.
- El método CP2-B debe considerar el FTS5 disponible en la versión objetivo y la migración/backfill de FTS; esta máquina no representa otras versiones de SQLite.
- Backup API no incluye Qdrant, cifrado, custodia, Portainer, ACL, snapshots del host ni una restauración operacional.
- Decisiones pendientes: journal mode oficial y política de WAL/checkpoint; timeout/lock de migrador; manifest/checksum y schemas legacy soportados; estrategia para reparar/restaurar los estados parciales que dejó un runner antiguo; backup cifrado/custodia; orden entre migraciones offline y aplicación; evidencia de host y FTS5.

**GO limitado** para aceptar los contratos experimentales de CP2-A y diseñar CP2-B. **NO-GO** para modificar `app/migrations.py`, añadir migration 29, ejecutar migración/bootstrap productivo, declarar `runtime-verified`, abrir ingress o activar auth. Si una DB real presenta esquema/ledger/objetos desconocidos, parar y preservar; no auto-reparar ni migrar.

## 10. Verificación

Comando focal: `./.venv/bin/python -m pytest -q tests/test_cp2a_sqlite_migration_safety.py`. Resultado a la última ejecución: **22 pruebas focales**, incluye SIGKILL POSIX en journal DELETE y WAL, manifests estructurales, concurrencia, fallos SQL, históricos, backup/restore y PRAGMA real en ruta temporal. La suite CP1 también se ejecuta porque se amplió el contrato de activation.

- `tests/test_cp2a_sqlite_migration_safety.py`: **22 passed**. `tests/test_cp1_security_contracts.py`: **15 passed**, 1 warning Starlette `BlockingPortal`.
- `./scripts/nexo check`: **380 passed, 1 skipped**, exit 0. Cinco warnings Python conocidos (Starlette `BlockingPortal`, FastAPI `on_event`) y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`; sin fallos.
- `git diff --check` y whitespace de los tres archivos nuevos: OK, sin diagnósticos.
- Archivos nuevos: `prototypes/sqlite_migration_safety.py`, `tests/test_cp2a_sqlite_migration_safety.py`, `docs/stage15b-3b-cp2a-sqlite-migration-safety.md`. Actualizados para integrar los contratos: `prototypes/security_contracts.py`, `tests/test_cp1_security_contracts.py`, `docs/stage15b-3b-cp1-security-contracts.md`.
- Worktree con sólo esos seis archivos CP1/CP2-A sin commit; sin cambios en `app/`, migraciones, `web/`, Docker o configuración. No push, merge ni deploy. Stash histórico preservado. CP2-B, CP3 y Stage 15C no iniciados.
