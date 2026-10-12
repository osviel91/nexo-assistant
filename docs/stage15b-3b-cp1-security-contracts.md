# NEXO Stage 15B.3B CP1: Security Contracts

- Modalidad: catálogo, contratos de aceptación y harness sintético; sin cambios de runtime
- HEAD de preflight: `5814ce876dcfada8d2e66c069f02cfe10e85a4b9`, rama `main`
- Estado inicial: worktree limpio; stash `b7d2662a55bb2519aadd4291a93400c689afb46c` presente e intacto
- Resultado: **GO limitado para CP1; NO-GO para producción, exposición o activación**

## 1. Evidencia Y Alcance

Este checkpoint define contratos reproducibles. No conecta la autenticación, el catálogo ni el guard con `app.main`; no modifica `app/`, `web/`, migraciones, Docker ni configuración. Las pruebas sintéticas demuestran propiedades de los prototipos y del contrato, no protección activa de NEXO.

Los estados de evidencia se mantienen separados:

| Estado | Significado en CP1 |
|---|---|
| `pending` | No hay evidencia aceptada para el gate requerido. |
| `prototype-verified` | Prueba aislada con FastAPI/SQLite temporal y dependencias sintéticas. No satisface gates de runtime u operación. |
| `runtime-verified` | Prueba de integración conectada al runtime y a sus límites reales. No satisface gates operacionales. |
| `operational-verified` | Evidencia verificada de instalación/topología/procedimiento y aprobación operacional. No se asigna por los tests CP1. |

Los gates comparan estos estados literalmente por requisito. No aceptan booleanos, no derivan `runtime-verified` de `prototype-verified` y no promueven un estado por completar otro gate.

Los informes 2B.1, 2B.2, 2B.3 y 2B.4 se revisaron. El stash se verificó con `git show --stat`; no se aplicó. No se abrió SQLite productiva ni se contactaron proveedores, Qdrant, Arbiter, MCP externo o servicios desplegados.

## 2. Catálogo HTTP

`prototypes/security_contracts.py` declara `CATALOG_VERSION = 1`. Cada contrato tiene ID estable versionado, método, ruta, clase de registro (`core`, `framework`, `module`, `mount`), política objetivo, permiso explícito, ownership, CSRF y requisitos adicionales. Los permisos no se infieren del verbo HTTP. `validate_catalog()` rechaza duplicados, permisos incompletos, CSRF incoherente y módulos sin nombre; `coverage_errors()` compara método+ruta y detecta faltantes y bindings no declarados, incluidas fronteras `MOUNT`.

Conteo contra el código de este HEAD, importado con data dir temporal y `NEXO_MODULES=attachments,decision-runtime` (sin ejecutar startup):

| Superficie | Actual | Contrato |
|---|---:|---:|
| Core, incluido fallback SPA | 65 | 65 |
| Módulo `attachments` | 1 | 1 |
| Módulo `decision-runtime` | 1 | 1 |
| FastAPI framework (4 rutas, GET y HEAD cada una) | 8 | 8 |
| **Bindings HTTP método/ruta** | **75** | **75** |
| `MOUNT /assets` (frontera, no método HTTP) | 1 | 1 |
| `GET /{path:path}` fallback SPA | incluido arriba | contrato `shell-ui-allowlist`, nunca wildcard público |

La app mostró 72 entradas de routing: 71 rutas HTTP y un mount. El fallback cuenta como binding HTTP; el mount se cuenta aparte. Este total coincide con 67 bindings de aplicación más 8 framework, según 2B.4.

Las cuatro combinaciones se prueban en dos superficies distintas. La prueba sintética construye FastAPI desde los contratos y valida el inspector/guard. Una prueba separada ejecuta cuatro subprocesos que importan `app.main` con `NEXO_MODULES` y `NEXO_DATA_DIR` temporales y coteja los bindings reales registrados contra el catálogo: ninguno = 73 HTTP; sólo `attachments` = 74; sólo `decision-runtime` = 74; ambos = 75. Esos subprocesos no ejecutan startup ni abren SQLite. Las rutas de módulos sin contrato producen binding no declarado; una ruta retirada o cuyo método cambió produce drift por método+ruta.

Contratos relevantes:

- `POST /api/decisions` permanece `admin(diagnostics:run)` cuando `decision-runtime` está habilitado.
- `/api/health` sólo tiene como objetivo futuro liveness mínimo y barato; el handler real actual no cumple ese contrato.
- `/assets` es una frontera pública explícita, limitada a archivos estáticos, sin datos de usuario.
- `GET /{path:path}` no es política pública. Sólo una allowlist de shell UI puede ser pública; `/api/*` desconocida responde 404 no-HTML.
- Las cuatro rutas generadas de documentación requieren admin o deshabilitación en producción; no son públicas por ser anónimas hoy.

La validación del snapshot existente 2B.1 detecta diferencias cuando se llama. Este contrato exige invalidar/rechazar el snapshot ante registro tardío; conectarlo al lifespan/readiness y al runtime queda pendiente de CP4.

## 3. Harness Y Pruebas

`tests/test_cp1_security_contracts.py` usa FastAPI sintética, `AdministrativePersistence` sobre SQLite temporal, identidades/credenciales sintéticas, callback de ownership y un harness inerte de efectos. Los contadores incluyen llamadas ASGI `receive`, lectura de body, multipart, dependency, apertura SQLite de dominio, escritura, creación de run, hook, tarea asíncrona, tool, MCP, egress de provider e inicio SSE. El endpoint sintético incrementa los contadores únicamente después de entrar; un wrapper cuenta las llamadas reales a `receive`, y la denegación anónima acredita que no consume body ni ejecuta dependencia/endpoint: todos los contadores siguen en cero.

No se guardan credenciales reales ni se llaman proveedores externos. Provider, tool y MCP son fronteras falsas que sólo contarían una invocación. Las pruebas no sustituyen las futuras pruebas sobre repositorios y handlers productivos.

Pruebas CP1 ejecutables:

```sh
./.venv/bin/python -m pytest -q tests/test_cp1_security_contracts.py
```

La suite focal contiene **15 pruebas**. Verifica cobertura del catálogo por método+ruta, las cuatro combinaciones de módulos en app sintética y cuatro importaciones independientes del registro real de `app.main`, método cambiado/ruta no declarada, la política fija de decisions, el fallback cerrado para API desconocida, declaración de dominios de ownership/SSE, separación de gates, denegación previa a dependencia/body/efectos, CSRF y revocación, error de almacenamiento, mount estático, registro tardío y revocación cooperativa entre efectos SSE.

También se reutiliza evidencia existente de 2B.3: sus 14 pruebas cubren explícitamente Host/Origin, permiso, CSRF, ownership sintético, error de almacenamiento, identidad de servicio sintética, scope/audience/delegación, método, rutas desconocidas, snapshot tardío y SSE. Esta evidencia permanece de prototipo; no convierte la API real en protegida.

## 4. Ownership, Transacciones Y CAS

`OWNERSHIP_ACCEPTANCE` enumera los dominios: conversaciones; mensajes/ramas; notebooks; fuentes/documentos; runs; approvals; interactions; traces/diagnósticos; preferencias; referencias AgentProfile-notebook. Cada dominio exige que el principal autorizado sea dueño, que recurso ajeno e inexistente sean indistinguibles cuando corresponda, y que el predicado de ownership forme parte del mismo límite de escritura. Approvals e interactions añaden transición one-shot CAS y autorización antes de herramienta/reanudación; referencias de notebook requieren validación transaccional.

Pruebas de aceptación pendientes de integración CP5/CP6:

- Leer/listar/actualizar/borrar con owner ajeno no revela contenido ni diferencia observable frente a inexistente, según el contrato de recurso.
- Escrituras filtran por owner en el mismo `UPDATE`/`DELETE` o transacción; no aceptan un callback previo como garantía contra TOCTOU.
- Rama hereda owner y referencias a notebook/perfil se validan atómicamente.
- Runs, eventos, approvals, interactions y traces derivan owner desde conversación/run en cada rama de consulta.
- Dos consumidores concurrentes de la misma approval/interaction: exactamente uno gana CAS; herramienta y reanudación sólo ocurren después de autorización/transición.
- Fallo de persistencia o commit impide efectos protegidos y no se informa éxito desde estado sólo en memoria.

Para SQLite + vector store, MCP, provider u otro sistema externo, commit SQL no implica atomicidad distribuida. El contrato requiere idempotencia/outbox/reconciliación donde aplique y no promete revertir un efecto externo ya iniciado.

Estas son especificaciones para CP5/CP6; CP1 no implementa consultas ownership-aware ni afirma que un harness genérico pruebe una carrera de repositorio real.

## 5. Streaming Y Delegación

`SSE_CHECKPOINTS` define autorización antes de persistir mensaje/run, antes de `http.response.start`, revalidación antes de cada efecto protegido, expiración/revocación, cancelación/desconexión, delegación limitada por scope/audience, ownership de conversación y fail-closed ante storage error.

La prueba CP1 ejecuta un `StreamingResponse` sintético: confirma el primer efecto, revoca la sesión y comprueba que el efecto siguiente no se ejecuta. Es revocación **cooperativa**; una operación externa ya iniciada no puede deshacerse instantáneamente. Denegar inicialmente significa respuesta HTTP de autorización antes de abrir SSE, no un evento SSE de error.

Pendiente: autorización antes de cada escritura/provider/RAG/tool/MCP real, persistencia inicial de chat/run, hooks/tareas, segundo chequeo pre-header en runtime, cancelación durable de run, disconnect del cliente a través del proxy y emisión/verificación real de credenciales delegadas. El callback sintético no autentica un workload entre procesos.

## 6. Gates

`integration_blockers()` sólo acepta `runtime-verified` en catálogo+guard conectados, ownership/CAS de dominio integrado, revalidación SSE/efectos y pruebas de integración aprobadas. `prototype-verified`, booleanos como `True` y claves como `integration_ready=True` no eliminan bloqueos.

`activation_blockers()` es cerrado por defecto y vuelve a exigir literalmente evidencia `runtime-verified` para todos los requisitos de integración/runtime, además de `operational-verified` para HTTPS/topología, backup/restore, recuperación administrativa y aprobación explícita. Un éxito sintético no puede satisfacerlos; completar integration-ready tampoco promueve ningún requisito operacional.

El gate de activación incluye además los contratos de migración `incomplete_migration_fails_closed`, `ledger_commits_with_schema`, `schema_validated_beyond_version`, `unknown_future_version_rejected`, `storage_failure_has_no_anonymous_fallback`, `incompatible_restore_fails_closed` y `bootstrap_requires_verified_schema_and_backup`. Cada uno requiere su propia evidencia `runtime-verified`; un ensayo de harness en CP2-A permanece `prototype-verified` y no despeja esos bloqueos.

No existe flag de bypass de autenticación o recuperación. Un resultado de pruebas CP1 no satisface automáticamente ningún gate runtime ni operacional.

## 7. Estado Y Dependencias

| Contrato | Estado CP1 | Siguiente evidencia necesaria |
|---|---|---|
| Catálogo, métodos, permisos, módulos y drift | Definido; cobertura verificada en catálogo, apps sintéticas y las cuatro importaciones reales de `app.main` (sin startup) | CP4: snapshot después de todo registro conectado a lifecycle/readiness real |
| Auth, CSRF, Host/Origin y deny-before-effects | Prototipo 2B.3 + harness CP1 | CP4: guard real pre-body, completo y fail-closed |
| Persistencia administrativa | Prototipo 2B.2 con SQLite temporal | CP2: migración/bootstrap/rollback/crash/restore autorizados y representativos |
| Ownership histórico y aislamiento por owner | Contrato definido; no runtime verificado | CP2 decisión/migración, CP5 consultas y writes ownership-aware |
| CAS de approval/interaction | Contrato definido; prototipo no prueba dominio real | CP5/CP6: transacción y carrera concurrente sobre repositorio integrado |
| Streaming, cancelación y delegación | Parte del límite cooperativo verificada en prototipo | CP6: chat/approval real y boundary antes de cada efecto |
| HTTPS, ingress, backup operativo y recovery | Requisitos definidos, instalación desconocida | CP7: evidencia del entorno y aprobación operacional explícita |

Dependencias CP2: autorización específica futura para el checkpoint; resolver diseño del migrador sin asumir atomicidad de `executescript`; definir esquema y ownership histórico con operador; probar fallos, reinicios y restore en copias sintéticas/representativas autorizadas. CP1 no inicia CP2 ni Stage 15C.

Decisiones abiertas permanecen según 2B.4: roles finales y policy version; alcance multiusuario/recursos compartidos; ownership histórico; semántica de preferencias legacy; operador host-side y recovery; topología/HTTPS/proxy real; límites de revocación y aislamiento de tools. No se promovió ni modificó Software Docs Shared.

## 8. Verificación Y Decisión

- Recuento real aislado: 75 bindings HTTP, 72 entradas de routing, 1 mount para ambos módulos; las cuatro combinaciones de módulos se importaron por separado con data dirs temporales, sin startup ni apertura de SQLite.
- Prueba focal: `15 passed`, 1 warning deprecación Starlette `BlockingPortal`; sin fallo.
- `./scripts/nexo check`: **358 passed, 1 skipped**, exit 0. Cinco warnings Python de deprecación ya existentes (Starlette `BlockingPortal`, FastAPI `on_event`) y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`; sin fallos.
- `git diff --check`: OK. Los tres archivos nuevos también pasaron `git diff --no-index --check /dev/null <archivo>` sin diagnósticos.
- Archivos de CP1: sólo estos tres paths; no se modificaron `app/`, `web/`, migraciones, Docker ni configuración. No hubo push, merge ni despliegue.
- Recomendación: **GO sólo para revisar CP1; NO-GO para implementación/runtime, exposición, activación, CP2 o Stage 15C sin autorización expresa**.
