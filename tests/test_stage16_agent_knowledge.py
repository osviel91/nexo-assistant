import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileService
from app.chunking import ChunkingConfig
from app.embeddings import EmbeddingBatch
from app.ingestion import NotebookIngestionService
from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService
from app.retrieval import RetrievalService
from app.vector_index import SQLiteVectorIndex


class Embeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[float(len(text)), 1.0] for text in texts], "deterministic")


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    async def aiter_lines(self):
        material = "\n".join(str(message.get("content", "")) for message in self.payload["messages"])
        answer = "Protocol ZAFIRO-731 defines the operational color of Nexo as amethyst [S1]." if "amethyst" in material else "The Notebook-specific fact is not available."
        yield "data: " + json.dumps({"choices": [{"delta": {"content": answer}}]})
        yield "data: [DONE]"


class Stream:
    def __init__(self, payload):
        self.response = Response(payload)

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class Client:
    payloads = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, method, url, headers, json):
        self.payloads.append(json)
        return Stream(json)


class Stage16AgentKnowledgeTests(unittest.TestCase):
    def test_four_execution_combinations_keep_agent_and_knowledge_separate(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_db, old_client = main.DB_PATH, main.httpx.AsyncClient
            old_notebooks, old_ingestion, old_retrieval = main.notebooks, main.ingestion, main.retrieval_service
            main.DB_PATH = root / "nexo.sqlite3"
            main.httpx.AsyncClient = Client
            main.startup()
            try:
                with main.db() as database:
                    database.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "MacMini", "http://provider", "secret", main.now()))
                    database.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("Cyber-Tiel", "p", "Cyber-Tiel", "[]"))
                repository = NotebookRepository(main.db, main.now)
                main.notebooks = NotebookService(repository, root / "sources")
                main.ingestion = NotebookIngestionService(repository, root / "sources", main.now, main.MAX_UPLOAD)
                notebook = main.notebooks.create(NotebookInput("Sabiduría de Murphy"))
                source = main.notebooks.add_file(notebook["id"], "Protocol", "protocol.md", "text/markdown",
                                                 b"Protocol ZAFIRO-731 defines the operational color of Nexo as amethyst.")
                main.ingestion.ingest(notebook["id"], source["id"])
                indexed = RetrievalService(repository, SQLiteVectorIndex(main.db, main.now), Embeddings(),
                                            ChunkingConfig(target_tokens=100, max_tokens=120))
                asyncio.run(indexed.index_source(notebook["id"], source["id"]))
                main.retrieval_service = lambda _client: indexed
                main.agent_profiles = AgentProfileService(AgentProfileRepository(main.db, main.now), main.module_registry.tool_catalog)
                main.agent_profile_resolver = main.AgentProfileResolver(main.agent_profiles.repository)
                profile = main.agent_profiles.create(AgentProfileInput(
                    name="Tiel", provider_id="p", model_id="Cyber-Tiel", system_instructions="You are Tiel. Preserve this Soul."))

                no_agent_no_notebook = self.run_chat(main, "one", None, None)
                tiel_no_notebook = self.run_chat(main, "two", profile["id"], None)
                no_agent_notebook = self.run_chat(main, "three", None, notebook["id"])
                tiel_notebook = self.run_chat(main, "four", profile["id"], notebook["id"])

                self.assertNotIn("amethyst", no_agent_no_notebook["answer"])
                self.assertNotIn("amethyst", tiel_no_notebook["answer"])
                self.assertIn("amethyst", no_agent_notebook["answer"])
                self.assertIn("amethyst", tiel_notebook["answer"])
                self.assertEqual(tiel_notebook["runtime"]["agent_profile_name"], "Tiel")
                self.assertEqual(tiel_notebook["runtime"]["resolved_model"], "Cyber-Tiel")
                self.assertEqual(tiel_notebook["runtime"]["resolved_model_name"], "Cyber-Tiel")
                self.assertTrue(tiel_notebook["runtime"]["system_instructions_applied"])
                self.assertEqual(tiel_notebook["runtime"]["notebook_id"], notebook["id"])
                self.assertEqual(tiel_notebook["runtime"]["knowledge_retrieval_applied"], True)
                self.assertEqual(tiel_notebook["runtime"]["retrieval_count"], 1)
                self.assertEqual(tiel_notebook["runtime"]["retrieval_status"], "completed")
                self.assertEqual(tiel_notebook["runtime"]["retrieval_result_count"], 1)
                self.assertTrue(tiel_notebook["runtime"]["grounding_applied"])
                self.assertGreater(tiel_notebook["runtime"]["grounding_chunks"], 0)
                self.assertGreater(tiel_notebook["runtime"]["grounding_context_chars"], 0)
                self.assertEqual(tiel_notebook["runtime"]["generation_model"], "Cyber-Tiel")
                self.assertTrue(tiel_notebook["runtime"]["soul_applied"])
                self.assertEqual(tiel_notebook["runtime"]["physical_message_roles"], ["system", "system", "user"])
                with main.db() as database:
                    chunk_id = database.execute("SELECT id FROM document_chunks LIMIT 1").fetchone()[0]
                self.assertEqual(tiel_notebook["citations"][0]["source_id"], source["id"])
                self.assertEqual(tiel_notebook["citations"][0]["chunk_id"], chunk_id)
                payload = tiel_notebook["payload"]
                self.assertEqual(payload["model"], "Cyber-Tiel")
                self.assertEqual(payload["messages"][0], {"role": "system", "content": "You are Tiel. Preserve this Soul."})
                self.assertEqual(payload["messages"][1]["role"], "system")
                self.assertIn("amethyst", payload["messages"][1]["content"])
                self.assertNotIn("amethyst", tiel_no_notebook["payload"]["messages"][0]["content"])
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.notebooks, main.ingestion, main.retrieval_service = old_notebooks, old_ingestion, old_retrieval

    def run_chat(self, main, content, profile_id, notebook_id):
        Client.payloads = []
        response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="Cyber-Tiel", content=content,
                                                      agent_profile_id=profile_id, notebook_id=notebook_id)))
        body = asyncio.run(self.collect(response.body_iterator))
        packets = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        done = next(packet for packet in packets if packet.get("done"))
        answer = "".join(packet.get("delta", "") for packet in packets)
        return {"answer": answer, "runtime": done["runtime"], "citations": done["citations"], "payload": Client.payloads[0]}

    async def collect(self, iterator):
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


if __name__ == "__main__":
    unittest.main()
