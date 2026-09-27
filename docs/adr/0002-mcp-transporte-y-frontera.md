# ADR 0002: Transporte MCP y frontera de confianza

- Estado: aceptado para Etapa 3A
- Fecha: 2026-09-28

## Contexto

Nexo se despliega como un contenedor y los servidores MCP pueden vivir en
otros contenedores o servicios. El runtime de agente ya conoce el contrato
general de herramientas y no debe acoplarse a un proveedor de herramientas.
MCP es una frontera de confianza: los nombres, schemas y resultados provienen
de servidores externos.

## Decisiones

1. La primera integración usa Streamable HTTP mediante el SDK oficial de MCP
   para Python. Es apropiado para servidores independientes y redes Docker; no
   requiere crear procesos hijos dentro del contenedor de Nexo.
2. `NEXO_MCP_SERVERS` es una lista JSON explícita con `id`, `url`, `enabled`,
   `allowed_tools` y `timeout`. No hay autodiscovery ni credenciales en el
   repositorio o en las respuestas de diagnóstico.
3. El módulo descubre con `tools/list`, valida schemas de entrada de objeto y
   registra únicamente la allowlist. La allowlist vacía no expone herramientas.
   Cada nombre se adapta a `mcp__<server_id>__<tool_name>` para evitar
   colisiones deterministas.
4. El adaptador registra `ToolDefinition` en `ToolRegistry`. `AgentRuntime`
   permanece agnóstico de MCP y conserva sus límites de rondas y salida.
5. Cada llamada tiene timeout por servidor. Fallos de conexión, protocolo,
   permisos, ejecución y timeout se convierten en diagnósticos o resultados
   seguros y no detienen Nexo, SearXNG ni otros servidores.
6. El diagnóstico de `/api/modules` expone estado operativo y códigos de error,
   nunca headers, tokens ni secretos.

## Consecuencias

- MCP es opcional y su ciclo de vida cierra los clientes al apagar Nexo.
- Solo se implementan tools MCP sobre HTTP. Resources, prompts, sampling,
  OAuth, stdio, UI administrativa y aprobaciones quedan fuera de 3A.
- La configuración requiere reiniciar el proceso; la administración dinámica
  pertenece a una etapa posterior.
