from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

from app.tools import EffectiveToolSet


CAPABILITY_TERMS = {
    "knowledge": {"vault", "knowledge", "conocimiento", "conocimientos", "documentación", "documentacion", "nota", "notas", "homelab"},
    "search": {"buscar", "busca", "search", "find", "investiga", "investigar"},
    "read": {"leer", "lee", "read", "contenido", "información", "informacion"},
    "browse": {"explora", "explorar", "browse", "navega"},
    "relationships": {"relación", "relaciones", "relaciona", "relacionar"},
    "maintenance": {"reindexa", "reindexar", "reindex", "indexa", "indexar", "embeddings", "mantenimiento"},
    "weather": {"aemet", "tiempo", "temperatura", "temperaturas", "clima", "pronóstico", "pronostico", "previsión", "prevision"},
    "web": {"internet", "web", "actual", "actuales", "novedades", "noticias"},
    "datetime": {"fecha", "hora", "día", "dia", "hoy", "mañana", "manana", "date", "time", "datetime", "timezone"},
    "visualization": {"gráfico", "grafico", "gráfica", "grafica", "tabla", "visualiza", "visualización", "visualizacion", "chart", "table", "visualization"},
    "user_input": {"pregúntame", "preguntame", "pregunta", "elige", "elección", "eleccion", "preferencia", "aclaración", "aclaracion"},
    "infrastructure": {"infraestructura", "homelab", "nodo", "nodos", "servidor", "servidores"},
    "diagnostics": {"diagnóstico", "diagnostico", "diagnostics", "herramientas", "tools", "mcp", "capacidad", "capacidades"},
}
DISCOVERY_TERMS = {"herramientas", "tools", "mcp", "disponibles", "disponible", "puedes", "capacidad", "capacidades", "enumera", "enumerar"}
NO_TOOL_TERMS = {"hola", "buenas", "gracias", "adiós", "adios", "explícame", "explicame", "explícame", "define"}
STOP = {"el", "la", "los", "las", "de", "del", "en", "mi", "un", "una", "y", "con", "sobre", "qué", "que", "como", "cómo", "por", "para", "haz", "me", "lo", "es", "esto", "tiene", "dice"}
KNOWLEDGE_BUNDLE = {"search", "read", "browse", "relationships"}


@dataclass(frozen=True)
class Selection:
    tools: EffectiveToolSet
    mode: str
    capabilities: tuple[str, ...]
    reasons: dict[str, tuple[str, ...]]
    fallback: bool
    discovery: bool
    duration_ms: float
    authorized_schema_chars: int
    selected_schema_chars: int


def enabled() -> bool:
    return os.getenv("NEXO_TOOL_SELECTOR", "true").strip().lower() in {"1", "true", "yes", "on"}


def max_tools() -> int:
    try:
        return min(100, max(1, int(os.getenv("NEXO_TOOL_SELECTOR_MAX_TOOLS", "10"))))
    except ValueError:
        return 10


def _text(tool: Any) -> str:
    return " ".join((tool.name, tool.module_id, tool.source, tool.description, str(tool.parameters))).casefold()


def _caps(tool: Any) -> set[str]:
    text = _text(tool)
    result = set()
    for cap, words in CAPABILITY_TERMS.items():
        if any(word in text for word in words):
            result.add(cap)
    if "mcp." in tool.name.casefold() or "vault" in text or "knowledge" in text:
        result.add("knowledge")
    if "search" in text or "buscar" in text:
        result.add("search")
    return result


def _schema_chars(tools: EffectiveToolSet) -> int:
    import json
    return len(json.dumps(tools.definitions(), ensure_ascii=False, separators=(",", ":")))


def select_tools(authorized: EffectiveToolSet, query: str, recent_context: str = "", *, active: bool | None = None, limit: int | None = None) -> Selection:
    started = time.perf_counter()
    full_chars = _schema_chars(authorized)
    active = enabled() if active is None else active
    limit = max_tools() if limit is None else max(1, limit)
    tools = authorized._tools
    if not active:
        chosen, mode, caps, reasons, fallback, discovery = tools, "disabled", (), {}, False, False
    else:
        query_text = re.sub(r"[._-]", " ", f"{query} {recent_context}".casefold())
        tokens = set(re.findall(r"[\wáéíóúüñ-]+", query_text)) - STOP
        discovery = bool(tokens & DISCOVERY_TERMS and ("?" in query or tokens & {"enumera", "enumerar", "disponibles", "disponible"}))
        caps = {cap for cap, terms in CAPABILITY_TERMS.items() if tokens & terms}
        explicit_names = {tool.name for tool in tools if re.sub(r"[._-]", " ", tool.name.casefold()) in query_text}
        scored: list[tuple[int, int, Any, set[str]]] = []
        has_weather_provider = any("weather" in _caps(tool) for tool in tools)
        reasons = {}
        for position, tool in enumerate(tools):
            text = re.sub(r"[._-]", " ", _text(tool))
            tcaps = _caps(tool)
            explicit = tool.name in explicit_names
            if "maintenance" in tcaps and "maintenance" not in caps and not explicit:
                continue
            if "knowledge" in tcaps and "knowledge" not in caps and not explicit:
                continue
            if tool.name == "web_search" and "knowledge" in caps and "web" not in caps and not explicit:
                continue
            if ("weather" in caps and "datetime" in tcaps
                    and not tokens & {"fecha", "hora", "date", "time", "datetime"}):
                continue
            score, why = 0, []
            if explicit:
                score += 100
                why.append("explicit_tool_match")
            match = caps & tcaps
            if match:
                score += 12 * len(match)
                why.append("capability_match")
            words = set(re.findall(r"[\wáéíóúüñ-]+", text))
            overlap = (tokens - set().union(*CAPABILITY_TERMS.values()) - STOP) & words
            if overlap:
                score += min(8, len(overlap) * 2)
                why.append("description_match")
            # Keep read/search/context siblings available for multi-step research, not maintenance.
            if "knowledge" in caps and "knowledge" in tcaps and (tcaps & KNOWLEDGE_BUNDLE) and "maintenance" not in tcaps:
                score += 9
                why.append("bundle_dependency")
            if "maintenance" in caps and "maintenance" in tcaps:
                score += 30
                why.append("capability_match")
            if "infrastructure" in caps and "visualization" in caps and "visualization" in tcaps:
                score += 12
                why.append("capability_match")
            if "user_input" in tcaps and "user_input" in caps:
                score += 24
                why.append("capability_match")
            if "weather" in caps and not has_weather_provider and tool.name == "web_search":
                score += 12
                why.append("capability_match")
            if score:
                reasons[tool.name] = tuple(dict.fromkeys(why))
                scored.append((score, -position, tool, tcaps))
        if discovery:
            chosen, mode, fallback = tools, "discovery", False
            reasons = {tool.name: ("discovery_mode",) for tool in tools}
        elif (tokens <= NO_TOOL_TERMS | STOP or tokens & NO_TOOL_TERMS) and not scored:
            chosen, mode, fallback = (), "no_match", False
        elif not tokens - set().union(*CAPABILITY_TERMS.values()) - STOP and not scored:
            chosen, mode, fallback = (), "no_match", False
        elif not scored:
            chosen, mode, fallback = tools, "fallback", True
            reasons = {tool.name: ("fallback",) for tool in tools}
        else:
            scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
            # Unclear research requests fail open to the authorized catalog.
            fallback = bool(tokens & {"investiga", "investigar", "analiza", "compara"} and not caps - {"search"})
            if fallback:
                chosen, mode = tools, "fallback"
                reasons = {tool.name: tuple(dict.fromkeys((*reasons.get(tool.name, ()), "fallback"))) for tool in tools}
            else:
                chosen = tuple(item[2] for item in scored[:limit])
                mode = "ranked"
    selected = EffectiveToolSet(tuple(chosen))
    return Selection(selected, mode, tuple(sorted(caps)), reasons, fallback, discovery,
                     round((time.perf_counter() - started) * 1000, 3), full_chars, _schema_chars(selected))
