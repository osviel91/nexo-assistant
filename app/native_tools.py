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

    context.tools.register(ToolDefinition("native.get_current_datetime", "Devuelve fecha y hora actual usando una zona IANA. Úsala cuando la respuesta dependa de fechas relativas como hoy, los próximos días o los últimos N días. La zona omitida es UTC; no la uses para una respuesta escalar innecesariamente.", {"type": "object", "properties": {"timezone": {"type": "string"}}, "additionalProperties": False}, get_datetime, "native", "native"))
    context.tools.register(ToolDefinition("native.render_artifact", "Crea una visualización cuando aporte claridad e incluye una interpretación textual. data debe ser: table={columns,rows}; bar/line/pie={labels,series:[{name,values:[numbers]}]} (usa values, no data); scatter={points:[{x,y}]}; metrics=[{label,value,unit?,delta?,trend?}] (es una lista); html={html}.", {"type": "object", "properties": {"type": {"enum": ["table", "bar", "line", "pie", "scatter", "metrics", "html"]}, "title": {"type": "string"}, "description": {"type": "string"}, "data": {"description": "table: object columns/rows; charts: object labels/series with numeric values; scatter: object points; metrics: array of label/value objects; html: object with html string.", "anyOf": [{"type": "object"}, {"type": "array"}]}, "options": {"type": "object"}}, "required": ["type", "title", "data"], "additionalProperties": False}, render, "native", "native"))
