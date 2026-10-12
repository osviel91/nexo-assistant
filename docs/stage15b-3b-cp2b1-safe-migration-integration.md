# NEXO Stage 15B.3B CP2-B.1: Safe SQLite Migration Integration

- HEAD de preflight: `71edebe7b6d3e8786816275b1d3efa6eaf2476b1`, rama `main`
- Estado inicial: worktree limpio; stash `b7d2662a55bb2519aadd4291a93400c689afb46c` intacto
- Modalidad: motor offline, promoción de un archivo SQLite y fail-closed de startup
- Resultado: **GO para consolidar CP2-B.1 en código; NO-GO operativo para Portainer/producción/exposición**
- Entrega: un único commit local, sin push, merge ni deploy

## 1. Cambios Integrados

`app/safe_migrations.py` implementa el flujo offline. No se añadieron migraciones ni se tocaron las funciones 1–28. No hay endpoint HTTP para migrar.

El comando futuro es explícito y exige atestación de servicio detenido:

```sh
python -m app.safe_migrations migrate --data-dir /ruta/absoluta/nexo-data --service-stopped
python -m app.safe_migrations recover --data-dir /ruta/absoluta/nexo-data --service-stopped
```

`app.main.startup()` ahora obtiene un lock compartido del data dir y valida el esquema runtime v28; no llama al runner histórico. `embedding_configuration()` y `knowledge_health()` ya no migran desde handlers. Si falta la base, hay journal de promoción, versión no soportada, manifest distinto o integridad/FK inválida, startup falla antes de servir rutas. El lock se conserva hasta que sale el proceso worker, incluso después de `shutdown()`, para no liberarlo mientras pueda quedar una tarea/conexión del mismo proceso.

Los tests de aplicación preparan explícitamente SQLite temporal con `tests/nexo_db_fixture.py`. Ese helper confina la ruta a `tempfile.gettempdir()` y no es importado por `app/`.

El código aún no activa autenticación. Con un schema válido, la API existente sigue siendo anónima; esta integración **no hace segura ni desplegable** la app.

## 2. Manifest Y Compatibilidad

El runtime construye y cachea en `:memory:` el manifest esperado ejecutando las funciones de schema del código, y compara con la DB persistente en read-only. No llama `migrations.migrate()`, no crea staging ni escribe el archivo durante startup. Se inspeccionan ledger/secuencia, conjunto exacto de objetos, SQL estructural normalizado, columnas (`table_xinfo`, incluidas generadas), metadata de índices, FK, `integrity_check` y `foreign_key_check`. La normalización ignora espacios, comentarios y casing de keywords no citados, pero preserva literales/identificadores citados y constraints; se prueba variante de formato y cambio de literal. `user_preferences` es la única extensión opcional permitida, con definición exacta conocida. Objetos no declarados, schema parcial, gap o versión futura no se aceptan.

**Compatibilidad pendiente:** las pruebas ejecutadas usan SQLite **3.49.2**. El template se genera con la SQLite activa, pero no se probó compatibilidad de migraciones/FTS ni equivalencia estructural en el SQLite del contenedor/Portainer. Si la versión difiere en virtual tables, SQL guardado o PRAGMA metadata, el manifest puede rechazarla; no se afirma que esa instalación pueda arrancar. No relajar diferencias sin fixture autorizada.

El helper interno para migraciones futuras `_apply_transactional_migration()` requiere un `DatabaseLease` exclusivo válido, conexión en el mismo data dir, ledger contiguo y validators de esquema anterior/posterior; ejecuta sentencias individuales, comprueba integridad/FK, escribe ledger y hace commit en una transacción. `SafeMigrationEngine` sólo invoca esos specs bajo su lease; `TRANSACTIONAL_MIGRATIONS` está vacío: no hay v29 ni schema administrativo. Una prueba aplica una migración sintética v29 bajo ese lease en SQLite temporal y verifica idempotencia/rollback; no es una migración registrada.

## 3. Staging Y Promoción

La herramienta requiere data dir existente y no symlink. El proceso offline obtiene un `flock(LOCK_EX | LOCK_NB)` sobre `nexo.sqlite3.migration.lock`; runtime mantiene `LOCK_SH` hasta que sale el worker, no sólo hasta el callback ASGI `shutdown()`. Si una app CP2-B.1 sigue viva, el migrador falla antes de leer o abrir la base. El lock sólo coordina versiones que implementen este mecanismo; `--service-stopped` sigue siendo una atestación para procesos/containers antiguos o consumidores externos.

Para base existente, el motor valida source, crea un backup por SQLite Backup API, valida backup, clona backup a staging y ejecuta migraciones 1–28 **sólo allí**. Las migraciones históricas con `executescript()` no se consideran atómicas: si el staging falla, se descarta; el original y backup quedan preservados. Se valida el schema destino y se exige archivo autocontenido sin sidecars.

Promoción está limitada a source en journal `DELETE` y sin `-wal`, `-shm` o `-journal`. Si el origen está en WAL o hay sidecars, se rechaza antes de modificarlo. Bajo el lock NEXO y `BEGIN EXCLUSIVE` en el inode source, el motor vuelve a comprobar estado y fingerprint de datos. Si no obtiene exclusión o detecta cambios desde el backup, no promueve. La sustitución es un único `os.replace()` del staging cerrado, en el mismo directorio, seguido de `fsync` del directorio y validación del archivo reabierto.

Antes del replace escribe atómicamente un journal de promoción con source/target, fingerprints y nombres confinados de backup/staging. El startup rechaza toda DB mientras exista journal. `recover` sólo finaliza si el canónico coincide exactamente con target, o cancela si coincide con source; cualquier tercer estado conserva evidencia y queda bloqueado para intervención. No intenta rollback con múltiples renombres ni repara ledger/schema manualmente.

**Garantía y límite:** pruebas POSIX con SIGKILL demuestran source intacto antes del replace y target completo tras el replace, bajo locks cooperativos y en filesystem temporal local. SQLite `BEGIN EXCLUSIVE` rechaza un reader activo en DELETE; el lock shared/exclusive coordina los procesos NEXO actualizados. Un proceso legacy/no cooperativo con un descriptor inactivo no puede detectarse completamente desde este runtime; detener esos procesos es una precondición operacional, no evidencia CP2-B.1. No se probó el filesystem del volumen Portainer, pérdida de energía, durabilidad hardware, reinicio de contenedor, backup cifrado ni Qdrant.

Un origen WAL y una base vacía de bytes se rechazan deliberadamente; la migración no checkpointa ni muta el origen para hacerlos promovibles. El backup en este prototipo queda en el mismo data dir con permisos 0600, sin cifrado ni custodia.

## 4. Evidencia CP1

`MIGRATION_EVIDENCE` se actualizó por requisito, sin promoción automática ni evidencia operacional:

| Contrato | Evidencia CP2-B.1 | Base de la evidencia |
|---|---|---|
| `incomplete_migration_fails_closed` | `runtime-verified` | Staging fallido se descarta; startup rechaza v27 y journal incompleto en fixtures `app.main` |
| `ledger_commits_with_schema` | `runtime-verified` | SIGKILL antes/después del replace: reapertura ve source completo o target+ledger completo |
| `schema_validated_beyond_version` | `runtime-verified` | `app.main.startup` rechaza v28 con tabla requerida ausente o extension drift |
| `unknown_future_version_rejected` | `runtime-verified` | Startup real de app con v29 sintética rechaza sin escribir el archivo |
| `storage_failure_has_no_anonymous_fallback` | `prototype-verified` | El startup falla si falta storage, pero no hay auth integrada ni prueba de request autenticado/storage failure |
| `incompatible_restore_fails_closed` | `runtime-verified` | Backup/restore v27 íntegro en temp; `app.main.startup` lo rechaza y no lo muta |
| `bootstrap_requires_verified_schema_and_backup` | `prototype-verified` | Sólo hay fixture CP2-A; bootstrap administrativo real aún no existe |

Los cinco estados `runtime-verified` describen integración del motor y `app.main` probada con SQLite temporal, no una instalación de producción. Storage-fallback y bootstrap siguen `prototype-verified`; ninguna propiedad se eleva a `operational-verified`. El runtime manifest local no prueba versión/configuración de Portainer.

## 5. Operación Y Riesgos

La operación documentada sólo aplica después de una autorización posterior: parar servicio/ingress y consumers, verificar `--data-dir`, ejecutar `migrate --service-stopped`, revisar backup/manifests y recovery, y arrancar únicamente si runtime validation pasa. El runtime computea el schema esperado en memoria, no migra al arrancar; se requiere verificar en una fase autorizada que la versión SQLite/FTS del contenedor produce un manifest compatible. No se debe desplegar CP2-B.1 para “probar” en Portainer.

Runbook futuro para Portainer, no ejecutado aquí: detener el servicio NEXO y cerrar ingress; verificar tag/digest de imagen y volumen nombrado `nexo-data`; ejecutar el módulo como comando one-shot de la misma imagen con el volumen montado en `/data` y `--data-dir /data --service-stopped`; no montar el socket Docker ni iniciar una segunda app. Si la orden falla, dejar el servicio detenido, conservar source/backup/staging/journal y pedir revisión. Tras éxito, arrancar exactamente el release correspondiente: el startup verifica schema y journal. No borrar el backup ni abrir ingress por el hecho de que el migrador haya devuelto éxito. La DB y el backup de CP2-B.1 contienen datos/credenciales en claro y 0600 no sustituye cifrado, ACL o custodia operacional.

Un crash anterior al journal puede dejar artifacts de backup/staging huérfanos; source permanece intacto y el siguiente comando elimina sólo staging namespaced. Backups previos se retienen y deben gestionarse bajo una política futura de cifrado/retención. Un journal corrupto, backup ausente o fingerprints que no coinciden requieren intervención: no hay recuperación automática con certeza suficiente. El motor rehúsa source WAL/sidecars; requiere un origen en DELETE sin sidecars y un lease de NEXO libre. La atestación `--service-stopped` no detecta procesos antiguos/no cooperativos ni reemplaza el stop real del stack.

La ruta `./scripts/nexo smoke` crea DB nueva y ahora fallará hasta que se ejecute la herramienta offline en su temp data dir; no se cambió el script porque el alcance CP2-B.1 excluye configuración/scripts. `dev` también requiere preparar DB antes del startup.

## 6. Pruebas Y Verificación

- `tests/test_cp2b1_safe_migrations.py`: **21 passed**. Incluye base v1/v12/v21/v27→v28, fresh/no-op, backup, rollback ante staging fault, detección de source cambiado tras backup, rechazo WAL con bytes/sidecars intactos, locks entre procesos independientes, liberación tras SIGKILL, guard del único helper de DB de la app, reader SQLite activo, startup fail-closed por schema/storage, ausencia de migración persistente por petición, SIGKILL/reinicio pre/post replace y validación de generación activa, recovery con backup inválido/journal ambiguo/source modificado, staging abandonado, manifest de `user_preferences`, versión futura/drift y helper transaccional sintético.
- CP1: `tests/test_cp1_security_contracts.py` mantiene los siete gates; la suite fue modificada sólo para verificar los niveles de evidencia explícitos.
- `./.venv/bin/python -m pytest -q tests/test_cp1_security_contracts.py`: **15 passed**.
- `./scripts/nexo check`: **401 passed, 1 skipped**, exit 0. Cinco warnings Python conocidos (Starlette `BlockingPortal`, FastAPI `on_event`) y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`; sin fallos.
- `git diff --check` y whitespace de todos los archivos nuevos: OK, sin diagnósticos.
- Cambios en `app/main.py`, `app/safe_migrations.py`, docs y pruebas/fixtures. No se tocó migraciones 1–28, `web/`, Docker, Compose, Portainer ni configuración. Sin push/merge/deploy; stash histórico intacto.
- CP2-B.2, CP3 y Stage 15C no iniciados.

## 7. Revisión De Garantías CP2-B.1

Las siguientes pruebas son reproducibles localmente: crean sus bases bajo `tempfile`, y donde se indica sincronizan procesos hijo con una barrera `READY` antes de enviar SIGKILL. No acceden a `$NEXO_DATA_DIR` del usuario ni a SQLite productivo.

| Garantía | Evidencia concreta | Nivel |
|---|---|---|
| SIGKILL antes/después de replace y detección de generación | `test_next_process_recovers_and_validates_active_generation_after_sigkill` mata al migrador en `before_replace`, `after_replace` y `after_validation`; un segundo proceso llama `migrate()` (sin `recover()` manual). Antes del replace observa v27 y migra; después observa y valida v28, limpia el journal y devuelve no-op. Startup rechaza el journal pendiente. | `runtime-verified` en SQLite temporal/procesos hijo |
| Exclusión worker/promotor y liberación | `test_independent_worker_and_promoter_exclude_each_other_and_sigkill_releases_lock` demuestra worker SH bloquea el motor EX, SIGKILL libera SH y el motor promueve; después, una promoción detenida en `before_replace` bloquea SH y SIGKILL libera EX. `test_database_helper_cannot_bypass_runtime_startup_lease` demuestra que `app.main.db()` rechaza acceso sin lease/startup válido. En `app/`, la única apertura de la DB de servicio es ese helper; las demás aperturas pertenecen al motor offline, manifest en memoria o staging. | `runtime-verified` local; sólo locks cooperativos |
| Startup fail-closed | `test_incompatible_runtime_states_never_start_api_or_mutate_database` prueba schema v27, versión futura v29, ledger con gap, schema parcial y objetos sin ledger mediante `TestClient`; startup impide entrar a la API, `migrations.migrate` está configurado para fallar si se llama y los bytes quedan idénticos. | `runtime-verified` local |
| Recuperación y promoción ambigua | `test_migration_cleans_abandoned_staging_and_validates_backup_integrity` valida backup con `inspect_database`, elimina staging abandonado y valida el target. `test_recovery_rejects_corrupt_backup_and_ambiguous_active_generation` rechaza backup corrupto, canonical alterado y journal con generaciones contradictorias; conserva journal/evidencia. | `runtime-verified` local |

`runtime-verified` significa que se ejercitó la integración real de `app.main`, locks POSIX y `SafeMigrationEngine` en el runtime local con SQLite temporal. `prototype-verified` permanece para `storage_failure_has_no_anonymous_fallback` y `bootstrap_requires_verified_schema_and_backup`; no existe el bootstrap administrativo ni auth que pruebe esos contratos. Ninguna de las cuatro garantías está `operational-verified`: no se probó el filesystem/SQLite del volumen Portainer, reinicio real de contenedor, fsync ante pérdida de energía, ni consumidores legacy/no cooperativos. La coordinación file-lock no puede obligar a un proceso externo que decida ignorarla; el stop efectivo de esos procesos sigue siendo requisito operacional.

**Recomendación:** GO para consolidación local de CP2-B.1 con este alcance y evidencia. NO-GO para Portainer/producción hasta probar compatibilidad de SQLite y atomicidad/fsync sobre el volumen real, comprobar la parada de todos los consumidores y resolver la custodia/cifrado del backup. NO se requiere cambiar infraestructura para la cobertura local; esas validaciones operativas no se simulan como aprobadas.
