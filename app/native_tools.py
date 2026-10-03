from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable

from app.artifacts import ArtifactError, validate_artifact
from app.kernel import ModuleContext, ToolDefinition, ToolExecutionContext


ARTIFACT_DATA_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["columns", "rows"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 1000},
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "values": {"type": "array", "items": {"type": ["number", "null"]}, "maxItems": 1000},
                        },
                        "required": ["values"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
            },
            "required": ["labels", "series"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["points"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"html": {"type": "string", "maxLength": 50000}},
            "required": ["html"],
            "additionalProperties": False,
        },
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "value": {}, "unit": {"type": "string"}, "delta": {}, "trend": {"type": "string"}},
                "required": ["label", "value"],
                "additionalProperties": False,
            },
            "maxItems": 30,
        },
    ]
}


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

    async def request_user_input(_execution: ToolExecutionContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"error": {"code": "interaction_unavailable", "message": "User input must be handled by the Agent runtime."}}

    context.tools.register(ToolDefinition("native.get_current_datetime", "Devuelve fecha y hora actual usando una zona IANA. Úsala cuando la respuesta dependa de fechas relativas como hoy, los próximos días o los últimos N días. La zona omitida es UTC; no la uses para una respuesta escalar innecesariamente.", {"type": "object", "properties": {"timezone": {"type": "string"}}, "additionalProperties": False}, get_datetime, "native", "native", action="read_only"))
    context.tools.register(ToolDefinition("native.render_artifact", "Crea una visualización con datos del usuario o fuentes consultadas. Envía argumentos como {type,title,data}; data es un objeto JSON para gráficos: {labels:[...],series:[{name,values:[números]}]}. Ejemplo: {type:'bar',title:'Ventas',data:{labels:['Ene','Feb'],series:[{name:'Ventas',values:[12,15]}]}}. bar/line admiten null para valores ausentes. Otros formatos: table={columns,rows}; scatter={points:[{x,y}]}; metrics=[{label,value,unit?,delta?,trend?}]; html={html}.", {"type": "object", "properties": {"type": {"enum": ["table", "bar", "line", "pie", "scatter", "metrics", "html"]}, "title": {"type": "string"}, "description": {"type": "string"}, "data": {**ARTIFACT_DATA_SCHEMA, "description": "Data structure for the selected artifact type: table object columns/rows; bar/line/pie object labels/series; scatter object points; metrics array; html object html."}}, "required": ["type", "title", "data"], "additionalProperties": False}, render, "native", "native", action="read_only"))
    context.tools.register(ToolDefinition("native.request_user_input", "Pause and ask the user for structured input, choices, or clarification; use this instead of asking in prose when the answer should guide the next step. Arguments: {dialog:{title,message?,submit_label?,fields:[{id,label,type,required?,default?,options?,min?,max?,placeholder?,help?}]}}. Field types: text, textarea, number, integer, boolean, select, multiselect, json.", {"type": "object", "properties": {"dialog": {"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string"}, "submit_label": {"type": "string"}, "fields": {"type": "array", "items": {"type": "object"}}}, "required": ["title"], "additionalProperties": True}}, "required": ["dialog"], "additionalProperties": False}, request_user_input, "native", "native", action="read_only"))
