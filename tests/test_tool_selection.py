import statistics
import time
import unittest

from app.kernel import ToolDefinition
from app.tools import EffectiveToolSet
from app.tool_selection import select_tools


def tool(name, description=""):
    async def handler(_context, _arguments):
        return {}
    return ToolDefinition(name, description, {"type": "object", "properties": {}}, handler, "mcp" if name.startswith("mcp.") else "native", "mcp" if name.startswith("mcp.") else "native", action="read_only")


CATALOG = EffectiveToolSet((
    tool("native.get_current_datetime", "current date time timezone"),
    tool("native.render_artifact", "create chart table visualization"),
    tool("native.request_user_input", "ask user clarification choice"),
    tool("aemet.opendata", "weather forecast temperature AEMET"),
    tool("web_search", "search the web current internet"),
    tool("mcp.knowledge-vault.search", "search knowledge vault notes"),
    tool("mcp.knowledge-vault.read", "read knowledge vault document"),
    tool("mcp.knowledge-vault.get_context", "get context knowledge vault"),
    tool("mcp.knowledge-vault.get_toc", "list table of contents knowledge vault"),
    tool("mcp.knowledge-vault.reindex", "reindex knowledge vault maintenance"),
    tool("mcp.knowledge-vault.build_embeddings", "build knowledge embeddings maintenance"),
    tool("mcp.knowledge-curator.consultar_contexto", "consultar contexto knowledge"),
))


class ToolSelectionTests(unittest.TestCase):
    def names(self, query, context="", **kwargs):
        return select_tools(CATALOG, query, context, **kwargs)

    def test_deterministic_evaluation_scenarios(self):
        cases = [
            ("hola", "", set()),
            ("¿Qué fecha y hora es?", "", {"native.get_current_datetime"}),
            ("¿Qué tiempo hará mañana en Madrid?", "", {"aemet.opendata"}),
            ("Haz un gráfico de barras con estos datos", "", {"native.render_artifact"}),
            ("Busca en mi vault información sobre ZimaBlade", "", {"mcp.knowledge-vault.search", "mcp.knowledge-vault.read"}),
            ("¿Y qué papel tiene la ZimaBlade?", "Busca la arquitectura del homelab en el vault", {"mcp.knowledge-vault.search", "mcp.knowledge-vault.read"}),
            ("Consulta el vault y crea una tabla con los nodos del homelab", "", {"mcp.knowledge-vault.search", "native.render_artifact"}),
            ("Consulta las temperaturas y haz un gráfico", "", {"aemet.opendata", "native.render_artifact"}),
            ("Busca en Internet las últimas novedades", "", {"web_search"}),
            ("Compara mi documentación sobre X con información actual de Internet", "", {"mcp.knowledge-vault.search", "web_search"}),
            ("¿Qué herramientas MCP tienes?", "", {tool.name for tool in CATALOG._tools}),
            ("Usa knowledge-vault-mcp para buscar arquitectura", "", {"mcp.knowledge-vault.search"}),
            ("Reindexa el vault", "", {"mcp.knowledge-vault.reindex"}),
            ("Busca ZimaBlade", "", set()),
        ]
        for query, context, required in cases:
            with self.subTest(query=query):
                result = self.names(query, context)
                self.assertTrue(required <= result.tools.names, (query, required - result.tools.names))
                if "Busca ZimaBlade" == query:
                    self.assertNotIn("mcp.knowledge-vault.reindex", result.tools.names)
                    self.assertNotIn("mcp.knowledge-vault.build_embeddings", result.tools.names)
                self.assertLessEqual(result.tools.names, CATALOG.names)
        ambiguous = self.names("Investiga esto")
        self.assertTrue(ambiguous.fallback)
        self.assertEqual(ambiguous.tools.names, CATALOG.names)

    def test_explicit_and_bundle_selection_never_escape_authorized_set(self):
        restricted = EffectiveToolSet((CATALOG.definition("mcp.knowledge-vault.search"),))
        selected = select_tools(restricted, "Usa knowledge-vault-mcp para buscar", limit=1)
        self.assertEqual(selected.tools.names, restricted.names)
        self.assertEqual(select_tools(restricted, "¿Qué herramientas MCP tienes?").tools.names, restricted.names)

    def test_disabled_mode_preserves_catalog(self):
        result = select_tools(CATALOG, "hola", active=False)
        self.assertEqual(result.mode, "disabled")
        self.assertEqual(result.tools.names, CATALOG.names)

    def test_catalog_scaling_benchmark(self):
        timings = {}
        for size in (10, 50, 100, 500):
            catalog = EffectiveToolSet(tuple(tool(f"mcp.server.tool_{index}", "search read knowledge document") for index in range(size)))
            samples = []
            for _ in range(30):
                started = time.perf_counter()
                select_tools(catalog, "Busca información en mi vault", limit=10)
                samples.append((time.perf_counter() - started) * 1000)
            ordered = sorted(samples)
            timings[size] = (statistics.median(ordered), ordered[int(len(ordered) * .95) - 1])
        self.assertTrue(all(p95 < 100 for _, p95 in timings.values()), timings)


if __name__ == "__main__":
    unittest.main()
