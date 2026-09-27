# ADR 0001: Fundamento modular integrado

- Estado: aceptado para Etapa 1
- Fecha: 2026-09-27

## Contexto

Nexo es un servicio FastAPI pequeno, sin build frontend, con SQLite y un unico
modulo backend. El chat, los proveedores/modelos, los adjuntos y el streaming
funcionan hoy, pero el arranque y las rutas estan concentrados en
`app/main.py`.

## Decisiones

1. El kernel conserva arranque/configuracion, persistencia y flujo base de chat,
   ademas del registro de modulos, compatibilidad de API, dependencias,
   lifecycle y hooks finitos. Los modulos no acceden a tablas ajenas.
2. El contrato es Python tipado con `dataclass` y `Protocol`; no se duplica un
   manifest JSON. El stack ya es Python y el registro ocurre en codigo
   integrado, no mediante importacion dinamica.
3. `ModuleRegistry` vive en `app/kernel.py`, separado de rutas y modelos de
   dominio para evitar dependencias circulares. El orden es el de registro y
   los errores se diagnostican por modulo sin abortar el kernel.
4. `NEXO_MODULES` activa una lista declarativa separada por comas. Su valor por
   defecto es `attachments` para conservar el comportamiento actual. Un modulo
   apagado no se registra, por lo que no tiene rutas ni extensiones de UI.
5. La API de modulos empieza en version `1`. Una incompatibilidad o dependencia
   ausente omite el modulo y deja un diagnostico; no bloquea chat ni arranque.
6. La primera capacidad extraida es `attachments`: tiene rutas y extraccion de
   archivos, no necesita SQLite y puede migrarse sin reescribir `/api/chat`.
   Su extension `attach-files` es el primer punto de interfaz delimitado.
7. Los hooks soportados son solamente `startup`, `shutdown`, `chat_before` y
   `chat_after`. No se introduce un bus generico.
8. La ejecucion de herramientas vive en `app/agent.py`, separada del endpoint
   HTTP. Cada handler recibe un `ToolExecutionContext`; el registro filtra las
   definiciones por capacidades del modelo. El runtime limita a tres rondas,
   acota el resultado serializado y convierte fallos en errores seguros.

## Estado de proveedores

Existe un unico adaptador OpenAI-compatible compartido por todos los
proveedores. Descubre modelos con `GET /models` y envia chat a
`/chat/completions`; no declara ni envia `tools`, vision o embeddings como
capacidades. Por tanto, esta iteracion no puede afirmar soporte de tool
calling: cada capacidad debera modelarse explicitamente antes de Etapa 2.

## Consecuencias

- Se mantienen SQLite, SSE, seleccion de modelo por mensaje, adjuntos y tema
  oscuro sin cambiar el despliegue.
- La configuracion se valida en el registro, pero no se crea todavia un
  sistema generico de secretos o migraciones de modulos porque no hay un
  consumidor real en Etapa 1.
- Las futuras integraciones MCP, Souls y RAG seran modulos integrados con
  contratos propios; no se implementan en este cambio.
