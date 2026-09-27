# ADR 0003: Runtime opcional de decisiones y frontera System One

## Estado

Aceptado para Etapa 3.5.

## Decisión

Nexo separa tres capacidades: `DECIDE` produce decisiones tipadas y
probabilísticas, `REASON` genera inferencia mediante `AgentRuntime`, y `ACT`
ejecuta herramientas mediante `ToolRegistry`/MCP. `DecisionRuntime` es una
capacidad paralela y no es una herramienta MCP ni una extensión de
`AgentRuntime`.

El módulo `decision-runtime` solo se registra cuando aparece en
`NEXO_MODULES`. Su runtime expone un lookup mínimo en `ModuleRegistry` bajo
`decision` y `decision-runtime`, y registra `POST /api/decisions` únicamente en
ese caso. El resto de Nexo no importa ni depende del runtime.

El contrato interno usa `state` y preguntas identificadas de tipo `boolean`,
`choice` o `score`. En el borde del primer adapter, `boolean` se traduce a
System One `noul`; `choice` y `score` conservan opciones, escalas,
probabilidades y confidence. La normalización vuelve a exponer un valor
tipado común en cada respuesta.

`ArbiterDecisionProvider` es solo un adapter HTTP configurable. La API pública
de TypeSafe documenta `POST /v1/systemone`, con `state`, `model` y un mapa de
preguntas `noul`/`choice`/`score`, y respuestas con probabilidades y confidence.
No se añade una dependencia de TypeSafe/Jev al core: el contrato refleja esos
conceptos, pero el provider concreto sigue siendo Arbiter. Laya queda detrás
del servicio remoto y no aparece en Nexo.

## Degradación

La disponibilidad del provider se comprueba de forma ligera durante startup y
se publica sin secretos en `GET /api/modules`. Con URL ausente, Arbiter caído,
timeout o respuesta inválida, Nexo continúa arrancando y el endpoint devuelve
un error controlado (`decision_provider_unavailable`, `decision_timeout`,
`decision_invalid_response` o `decision_invalid_request`). No se registra el
state, prompts completos, API key ni Authorization.

## Consecuencias

El runtime puede recibir posteriormente un `JevProvider` sin cambiar su
contrato ni el orquestador. No se implementan routing automático, selección de
modelos/herramientas, approvals, guardrails, UI, persistencia o entrenamiento
en esta etapa.

## Referencias

- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/introduction/quickstart
- https://docs.typesafe.ai/primitives/choice
- https://docs.typesafe.ai/primitives/score
- https://docs.typesafe.ai/primitives/noul
