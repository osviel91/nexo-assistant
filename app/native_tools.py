from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable

from app.artifacts import ArtifactError, validate_artifact
from app.kernel import ModuleContext, ToolDefinition, ToolExecutionContext


def register_native_tools(context: ModuleContext, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    async def get_datetime(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        zone_name = arguments.get("timezone", "UTC")
        if not isinstance(zone_name, str):
            return {"error": {"code": "invalid_timezone", "message": "Zona horaria IANA inválida."}}
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return {"error": {"code": "invalid_timezone", "message": "Zona horaria IANA inválida."}}
        value = clock().astimezone(zone)
        return {"iso": value.isoformat(), "date": value.date().isoformat(), "time": value.strftime("%H:%M:%S"), "timezone": zone_name, "weekday": value.strftime("%A"), "unix": int(value.timestamp())}

    async def render(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            artifact = validate_artifact(arguments)
        except ArtifactError as error:
            return {"error": {"code": "invalid_artifact", "message": str(error)}}
        return {"content": "Artefacto generado.", "structured_data": {"type": artifact["type"], "title": artifact["title"]}, "artifacts": [artifact]}

    context.tools.register(ToolDefinition("native.get_current_datetime", "Devuelve fecha y hora actual usando una zona IANA. La zona omitida es UTC. No la uses para una respuesta escalar innecesariamente.", {"type": "object", "properties": {"timezone": {"type": "string"}}, "additionalProperties": False}, get_datetime, "native", "native"))
    context.tools.register(ToolDefinition("native.render_artifact", "Crea una visualización declarativa cuando mejora materialmente la respuesta (comparaciones, tendencias, muchas filas o resumen KPI); no para un escalar simple. Incluye interpretación textual.", {"type": "object", "properties": {"type": {"enum": ["table", "bar", "line", "pie", "scatter", "metrics", "html"]}, "title": {"type": "string"}, "description": {"type": "string"}, "data": {"type": "object"}, "options": {"type": "object"}}, "required": ["type", "title", "data"], "additionalProperties": False}, render, "native", "native"))
