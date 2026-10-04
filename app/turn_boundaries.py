from __future__ import annotations


FOLLOW_UP_MARKERS = ("¿y ", "y también", "además", "tambien", "continúa", "continua", "continue", "follow up", "that search", "those results", "what about", "and for", "profundiza", "ese nodo", "ese servicio", "eso", "él", "ella", "la anterior", "lo anterior", "pasado mañana", "tomorrow")
NEW_DOMAINS = {
    "weather": ("tiempo", "temperatura", "pronóstico", "pronostico", "aemet"),
    "datetime": ("qué fecha", "que fecha", "qué hora", "que hora", "hoy"),
    "web": ("internet", "web", "noticias", "novedades"),
    "visualization": ("gráfico", "grafico", "tabla", "visualiza"),
    "knowledge": ("vault", "documentación", "documentacion", "homelab", "zimablade"),
}


def is_contextual_followup(current: str, previous: str) -> bool:
    current, previous = current.casefold().strip(), previous.casefold()
    if any(term in current for term in ("olvida eso", "olvídalo", "olvidalo", "cambiando de tema")):
        return False
    previous_domains = {name for name, terms in NEW_DOMAINS.items() if any(term in previous for term in terms)}
    current_domains = {name for name, terms in NEW_DOMAINS.items() if any(term in current for term in terms)}
    if current_domains and previous_domains and current_domains.isdisjoint(previous_domains):
        return any(marker in current for marker in FOLLOW_UP_MARKERS)
    return any(marker in current for marker in FOLLOW_UP_MARKERS)


def history_for_turn(history: list[dict], current: str) -> list[dict[str, str]]:
    """Retain failed turns as labeled context, not active provider user requests."""
    output = []
    failed_users = [(index, row) for index, row in enumerate(history)
                    if row.get("role") == "user" and row.get("runtime_status") in {"failed", "error"}]
    latest_failed = failed_users[-1][1] if failed_users else None
    contextual = bool(latest_failed and is_contextual_followup(current, latest_failed["content"]))
    for index, row in enumerate(history):
        role, content = row["role"], row["content"]
        if row.get("runtime_status") in {"failed", "error"} and role == "user" and not (contextual and latest_failed is row):
            role = "system"
            content = f"Previous user request ended without an answer; historical context only, do not resume unless the current user asks: {content[:1200]}"
        output.append({"role": role, "content": content})
    return output
