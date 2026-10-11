# Stage 15B.0: Repository Reconciliation and Migration Audit

- Estado: informe para revisión; no se integró el stash
- Fecha: 2026-10-11
- Alcance: reconciliar el trabajo local preservado contra `origin/main`; sin OAuth ni cambios funcionales

## Resumen ejecutivo

`origin/main` ya contiene la mayor parte del trabajo funcional de Stage 13A: `max_tool_calls` por Agent, bindings Agent-notebook, resolución de notebook para ejecución, precedencia, rechazo de múltiples bindings ambiguos y pruebas de persistencia/grounding. El stash local implementó capacidades muy similares, pero con otra tabla de bindings y una columna extra en la selección de tools.

No se debe restaurar el stash completo. Sus cambios en `app/agent.py`, `app/agent_profiles.py` y `app/main.py` son anteriores a las implementaciones actuales de aprobaciones durables, reanudación, ProgressGuard y ToolSelector; copiarlos completos regresaría funcionalidad reciente. La migration 22 local es incompatible con la migration 22 publicada si ya fue ejecutada en alguna instalación: SQLite registra sólo el número de versión, por lo que la migration 22 de `origin/main` se saltaría y faltaría la tabla `agent_profile_notebooks` que el código actual consulta.

El fast-forward de `main` no se ha ejecutado. La forma segura es alinear la rama dejando el stash archivado, recuperar después sólo documentación y pruebas reescritas para los contratos actuales. El stash y el respaldo externo deben conservarse hasta validar esas recuperaciones.

## Estado verificado

- Rama: `main`.
- HEAD local: `cce925b7a6add36cd6e3382c8f8b8c9e846cadfa` (`fix: expose native tools to agent runs`).
- `origin/main`: `29cafbe97bb6a5ef1dc937120b0349e7f5c3c9f0` (`feat: add semantic AEMET forecast operation`).
- Divergencia: `git rev-list --left-right --count HEAD...origin/main` da `0 20`; no hay commits locales exclusivos.
- Worktree después del checkpoint: limpio, sin cambios tracked ni untracked.
- Stash preservado: `stash@{0}`, commit `b7d2662a55bb2519aadd4291a93400c689afb46c`, mensaje `checkpoint: preserve pre-alignment worktree cce925b7`.
- El stash contiene los diez archivos enumerados abajo: ocho archivos tracked modificados y dos documentos untracked.
- Respaldo externo verificado: `/var/folders/08/mvm4rmsj5s5ff2gkdmk5wd2m0000gn/T/opencode/nexo-alignment-cce925b7/`. `SHA256SUMS` pasó y los dos documentos copiados coinciden byte a byte.
- La base local ignorada `data/nexo.sqlite3` se consultó en modo read-only, sin leer filas de aplicación: máximo de `schema_migrations` = 11; no contiene `agent_profile_notebooks`, `agent_knowledge_bindings` ni la columna `agent_profile_tools.enabled`.
- No se inspeccionó una base desplegada en Portainer; este checkout no demuestra el estado del volumen remoto. No se modificó ninguna base de datos.
- Una simulación previa de merge del stash con `origin/main` reportó conflictos en siete archivos; por eso se mantuvo la rama sin actualizar. No se aplicó ni eliminó el stash.

## Inventario y clasificación

Las clasificaciones indican qué conviene hacer con el delta local, no si el archivo completo es útil o correcto en el árbol actual.

| Archivo en stash | Delta local preservado | Equivalencia en `origin/main` | Clasificación | Decisión de recuperación |
| --- | --- | --- | --- | --- |
| `app/agent.py` | Hace que `AgentRuntime` use el límite `max_tool_calls` de `EffectiveRunConfiguration`. | Ya soportado. El runtime actual selecciona el límite per-Agent y además contiene el ciclo de recuperación, ProgressGuard, interacciones y continuidad de aprobaciones. | `SUPERSEDED` | No recuperar el archivo/diff. El comportamiento requerido existe en el runtime más reciente. |
| `app/agent_profiles.py` | Añade notebooks y límite de llamadas al modelo de perfil; enlaza y resuelve los notebooks; filtra tools por `enabled=1`. | `origin/main` ya persiste `notebook_ids` en `agent_profile_notebooks`, `max_tool_calls`, valida referencias, devuelve metadata y resuelve `requested_notebook_ids`. La selección de tools continúa representada por `tool_names`, no por una bandera enabled por fila. | `SUPERSEDED` | No recuperar. Reaplicar también regresaría validación selectiva de referencias: la versión actual permite editar otros campos aunque un modelo/notebook previo ya no esté disponible. |
| `app/main.py` | Añade fallback de notebook de perfil cuando el request omite Notebook, error 409 ante múltiples bindings ambiguos y pasa el límite al snapshot de ejecución. | Las tres decisiones están en la ruta actual. El contrato actual además captura herramientas solicitadas/disponibles/efectivas, historial de turnos y estado usado por ProgressGuard/aprobaciones. | `SUPERSEDED` | No recuperar el archivo/diff. |
| `app/migrations.py` | Migration 22 añade `max_tool_calls`, `agent_profile_tools.enabled` y `agent_knowledge_bindings(enabled)`. | La migration 22 actual añade `max_tool_calls` y crea `agent_profile_notebooks`; las migrations 23–28 crean interacciones, estado de tools por conversación, aprobaciones durables y `read_only_hint` MCP. | `INCOMPATIBLE` | No ejecutar ni renumerar esta migration. No sustituir ni sobrescribir las migrations 22–28 actuales. |
| `tests/test_agent_profiles.py` | Prueba persistencia de múltiples notebooks, límite por Agent y cascada de borrado. | La suite actual ya prueba notebooks y límite, incluida actualización de bindings, límite efectivo y cascade al borrar un notebook. | `SUPERSEDED` | No copiar la prueba duplicada. |
| `tests/test_migrations.py` | Prueba preservación de perfil/tool/notebook alrededor de la migration local 22 y espera schema version 22. | La suite actual espera versión 28 y la migration 22 oficial; no se encontró una prueba igual que verifique preservación explícita de un AgentProfile legacy al pasar de 21 a 28. | `REUSABLE` | Recuperar sólo la intención de preservación como nueva prueba contra `agent_profile_notebooks`, `max_tool_calls` y versión 28; nunca mantener la aserción de versión 22. |
| `tests/test_stage16_agent_knowledge.py` | Añade evidencia de retrieval/cita mediante notebook ligado al perfil y prueba que múltiples bindings sin selector producen 409. | La prueba actual ya cubre perfil con un notebook, grounding/citas y el flujo CHAT/Agent; la ruta implementa el 409, pero no se encontró esa regresión específica en la versión publicada. | `REUSABLE` | Recuperar de forma selectiva el caso multi-binding/409, adaptado a `agent_profile_notebooks` y al setup actual. Evitar reemplazar el fixture/prueba vigente completa. |
| `tests/test_stage6b_profile_runtime.py` | Prueba que un perfil restringido no expone tools de módulo/MCP no seleccionadas, mientras permite natives intrínsecas. También cambia argumentos de construcción de `ToolDefinition`. | La arquitectura actual conserva la intersección de autorización/perfil y native tools, pero añade `native.request_user_input`, ToolSelector y cambios de acción/policy. | `REUSABLE` | Recuperar sólo la intención de no elevar tools no seleccionadas; reescribirla sobre el conjunto autorizado y selección efectiva actuales. No conservar la lista fija anterior de dos native tools ni los argumentos posicionales si ya no son el contrato de prueba. |
| `docs/stage13a-agent-product-model.md` | Documenta límite, selección de tools, binding de knowledge y metadata. También declara `enabled` en `agent_profile_tools` y bindings de knowledge. | `docs/stage13a-agents.md` de `origin/main` documenta el mismo producto y las semánticas actuales, pero usa la relación `agent_profile_notebooks` y no afirma enabled por fila para tools. | `DOCUMENTATION` | No publicar una copia literal duplicada. Archivar con el stash; si se quiere conservar detalle, reconciliarlo en la documentación Stage 13A vigente y retirar afirmaciones de esquema obsoletas. |
| `docs/stage15a-chatgpt-oauth-feasibility.md` | Evaluación SIWC/Responses y restricciones del callback loopback, seguridad y compatibilidad. | No hay equivalente en el inventario de `origin/main`; no cambia código ni esquema. | `DOCUMENTATION` | Recuperar sin modificar su contenido después del fast-forward, conservarlo para Stage 15B y revalidar enlaces/política al retomar el trabajo. |

## Stage 13A contra `origin/main`

### Contratos que ya están implementados

- `AgentProfileInput` y `AgentRunConfiguration` actuales exponen `notebook_ids` y `max_tool_calls`.
- El CRUD de Agents persiste la configuración; el resolver crea un snapshot con selección de proveedor/modelo, herramientas solicitadas, notebooks solicitados y límite per-Agent.
- La migration 22 publicada añade `agent_profiles.max_tool_calls` con default 10 y rango 1–50, y crea `agent_profile_notebooks` con FK/cascade y clave primaria `(agent_profile_id, notebook_id)`.
- La ejecución prioriza notebook explícito del request, luego notebook ya vinculado a la conversación, luego la selección del perfil cuando corresponde. Múltiples bindings del perfil sin una selección concreta producen `409 agent_multiple_notebooks_unsupported`; retrieval sigue siendo single-notebook.
- `EffectiveRunConfiguration` actual contiene Agent, notebook efectivo, tools, grounding, temperatura, KnowledgeOutcome, tools solicitadas/disponibles y límite. Los límites del Agent y CHAT tienen diferentes fuentes.
- `docs/stage13a-agents.md` ya registra estas decisiones y las fronteras de selección/ejecución de tools.

### Diferencias que no deben copiarse sin diseño

- El nombre de la relación local `agent_knowledge_bindings` no coincide con el canónico publicado `agent_profile_notebooks`.
- El stash agrega `enabled` a `agent_profile_tools` y a `agent_knowledge_bindings`. La API del stash acepta una lista de `tool_names`/`notebook_ids` y reemplaza la lista entera; no ofrece una operación/contrato para editar enabled individualmente. En el código guardado, el estado enabled de tool no es administrable por la API.
- `agent_profile_notebooks` actual expresa bindings presentes y activos; el stash expresa filas que además podrían deshabilitarse. No es un cambio de nombre puramente mecánico si alguna base contiene filas deshabilitadas.
- El update del stash vuelve a validar todos los modelos y notebooks al editar cualquier campo. El servicio actual valida las referencias que se están cambiando, evitando bloquear cambios de descripción por referencias previas no disponibles. No regresar a la validación más restrictiva accidentalmente.
- El `EffectiveRunConfiguration` del stash pasa `max_tool_calls` como argumento posicional número siete. En `origin/main`, esa posición corresponde a `requested_tool_names`; el límite es otro campo posterior. Copiar la llamada de forma aislada puede cambiar silenciosamente el tipo/semántica del snapshot.
- El test guardado de tools espera sólo `native.get_current_datetime` y `native.render_artifact`; `origin/main` también incluye `native.request_user_input` y puede aplicar ToolSelector. Las expectativas deben basarse en el conjunto efectivo actual, no en una lista histórica fija.

## Auditoría de migraciones

### Migration 22 local y migrations 22–28 publicadas

| Versión | Stash local | `origin/main` | Evaluación |
| --- | --- | --- | --- |
| 22 | `agent_profiles.max_tool_calls`; `agent_profile_tools.enabled`; tabla `agent_knowledge_bindings` con `enabled`. | `agent_profiles.max_tool_calls`; tabla `agent_profile_notebooks` con FK/cascade. | Colisión real de número y de tabla. `max_tool_calls` coincide en intención; relación y enabled flags divergen. |
| 23 | No existe. | `runtime_interactions` para entradas/respuestas interactivas temporales. | No modificar; pertenece al runtime actual. |
| 24 | No existe. | `conversations.tools_enabled`. | No modificar. |
| 25 | No existe. | `conversations.web_enabled`. | No modificar. |
| 26 | No existe. | `runtime_interactions.tool_name` y `conversation_tool_approvals`. | No modificar. |
| 27 | No existe. | `pending_approvals` con continuación, fingerprint, argumentos congelados, estados y resolución. | No modificar. |
| 28 | No existe. | `mcp_tools.read_only_hint`. | No modificar. |

Las migrations de SQLite se marcan por número, no por hash/contenido. Si una DB registró la **migration 22 del stash**, al abrirla con el código de `origin/main`:

1. La migration 22 oficial se consideraría aplicada y se omitiría.
2. Las migrations 23–28 podrían continuar.
3. El runtime actual intentaría consultar `agent_profile_notebooks`, que esa DB no tendría, porque la versión local creó `agent_knowledge_bindings`.
4. Los datos de `agent_knowledge_bindings` no se copiarían automáticamente; tampoco se debe borrar la tabla/columna enabled por conjetura.

Ese escenario exigiría una migración de compatibilidad **aditiva y posterior** (con destino/número acordado y detección segura de esquema), previa inspección de la instalación. No se debe reescribir la migration 22 publicada ni bajar/renumerar el ledger.

En el SQLite local ignorado consultado en modo read-only, `MAX(schema_migrations.version)=11` y no existen ninguna de las dos tablas nuevas ni `agent_profile_tools.enabled`. Eso confirma que **ese archivo local** no aplicó la migration del stash. No confirma el estado del volumen Portainer ni de otra instalación. El stash no modifica bases por sí mismo; no hay evidencia aquí de que esta migration local se haya desplegado.

### Intervención de base de datos

- Para la DB local observada en versión 11, el código publicado puede avanzar mediante su secuencia normal de migrations 12–28; no hay estructuras del stash que reconciliar.
- No se puede declarar segura una intervención para cada DB desplegada sin inspeccionar su `schema_migrations` y `sqlite_master` de forma read-only en esa instalación.
- Antes de publicar/actualizar una instancia, comprobar si registró v22 y si tiene `agent_knowledge_bindings`, `agent_profile_notebooks` y `agent_profile_tools.enabled`. Si sólo fue migrada por el código publicado, no necesita tratamiento especial por Stage 13A.
- No ejecutar migrations durante esta fase ni copiar/mutar `data/nexo.sqlite3`.

## Compatibilidad con el runtime publicado

- **EffectiveRunConfiguration:** el objetivo funcional de pasar el límite de Agent ya existe; la tupla se amplió para conservar requested/available tool sets. La posición posicional local no corresponde al contrato publicado.
- **AgentRuntime:** la versión actual maneja ProgressGuard, intentos/respuestas, clasificación de policies, ejecución tras aprobación y continuación durable. Reemplazarla con la del stash eliminaría estados e invariantes de Stage 13C/14.
- **Aprobaciones y reanudación:** ahora se persisten en `pending_approvals`, se congelan y verifican argumentos/tool fingerprint y reanudan turns. La versión local sólo devuelve `approval_required` al stream; no contiene este lifecycle durable.
- **ProgressGuard:** el runtime actual registra progreso, recuperaciones y hard stops; el stash no lo contiene.
- **ToolSelector:** el runtime actual reduce tools autorizadas usando selección contextual. El filtro del perfil debe seguir siendo una frontera anterior a selección; no reintroducir el conjunto serializado de una ejecución previa.
- **AgentProfile:** el schema/API actual ya contiene notebooks y límite. Mantener la política de validación selectiva de referencias y la relación `agent_profile_notebooks`.
- **RAG/KnowledgeOutcome:** la ruta publicada resuelve el notebook y conserva retrieval, grounding, estado de conocimiento y citas; Stage 13A no necesita reimplementarlo. Deben añadirse pruebas focales, no reescribir la ruta.
- **SSE y tools:** los cambios de `main.py` en el stash preceden eventos de interacción, `waiting_approval`, reanudación y error handling actual. Evitar cherry-pick de bloques grandes o resolución automática por archivo.

## Plan de recuperación selectiva

No se ha ejecutado este plan:

1. Mantener `stash@{0}` y el respaldo externo como copias intactas. No usar `stash pop`.
2. Tras aprobar este informe, confirmar `git status --short --branch` limpio y usar únicamente `git merge --ff-only origin/main`. No hace falta merge commit: `HEAD` no tiene commits exclusivos.
3. Verificar el nuevo HEAD y que `git rev-list --left-right --count HEAD...origin/main` sea `0 0`.
4. Recuperar `docs/stage15a-chatgpt-oauth-feasibility.md` desde el tercer parent del stash, sin traer código del stash.
5. Conservar `docs/stage13a-agent-product-model.md` en el archivo del stash; si se necesita, reconciliar contenido útil con `docs/stage13a-agents.md`. No mantener dos contratos duplicados ni publicar la afirmación de `agent_profile_tools.enabled` sin decisión explícita.
6. En una tarea de código posterior, portar sólo tres intenciones de pruebas al contrato actual: (a) migrar un AgentProfile legacy y preservar instrucciones/tools con `max_tool_calls=10` y sin binding inventado; (b) probar `409 agent_multiple_notebooks_unsupported`; (c) probar que Agent tool selection nunca expone tools fuera del perfil, conservando nativas y ToolSelector actuales.
7. Ejecutar las pruebas focales primero y luego `./scripts/nexo check`; revisar cambios, esquema y estado antes de retirar cualquier respaldo. La migration 22 del stash no forma parte del plan de recuperación.
8. Mantener el stash hasta que los tests adaptados pasen y se acepte formalmente la disposición de los diez archivos.

## Criterios de no-regresión para la integración futura

- El código actual de aprobación, resume, ProgressGuard y ToolSelector se conserva sin copiar archivos enteros del stash.
- No se crea otra migration 22, no se cambian los números 22–28 y no se ejecutan escrituras sobre SQLite como parte de la reconciliación.
- Los tests recuperados usan `agent_profile_notebooks`, schema version 28 y el contrato actual de `EffectiveRunConfiguration`.
- El escenario real de `agent_knowledge_bindings` sólo se migra si se confirma que alguna instalación lo aplicó; la transformación de datos y de enabled flags requiere una decisión/migration nueva y explícita.
- Stage 15A se recupera como documentación; no implica aprobación para Stage 15B ni OAuth.

## Cierre

Estado al generar este informe: `main` sigue en `cce925b7a6add36cd6e3382c8f8b8c9e846cadfa`, 20 commits detrás de `origin/main`; `stash@{0}` y el respaldo externo siguen intactos; worktree previo a este informe estaba limpio. No se hizo fast-forward, no se aplicó stash, no se modificó funcionalidad ni SQLite y no se ejecutaron tests.

Detenerse para revisión antes de la sincronización y de cualquier Stage 15B.
