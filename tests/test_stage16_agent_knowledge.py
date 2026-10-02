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
        question = next((str(message.get("content", "")) for message in self.payload["messages"] if message.get("role") == "user"), "")
        answer = ("La ley de perversidad de la naturaleza dice que no se puede determinar a priori en qué lado de la tostada hay que poner la mantequilla [S1]."
                  if "perversidad" in question and "perversidad" in material else
                  "Protocol ZAFIRO-731 defines the operational color of Nexo as amethyst [S1]." if "amethyst" in material else
                  "The Notebook-specific fact is not available.")
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
                    database.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("Raw", "p", "Raw", "[]"))
                repository = NotebookRepository(main.db, main.now)
                main.notebooks = NotebookService(repository, root / "sources")
                main.ingestion = NotebookIngestionService(repository, root / "sources", main.now, main.MAX_UPLOAD)
                notebook = main.notebooks.create(NotebookInput("Sabiduría de Murphy"))
                source = main.notebooks.add_file(notebook["id"], "Protocol", "protocol.md", "text/markdown",
                                                 b"Protocol ZAFIRO-731 defines the operational color of Nexo as amethyst. LEY DE LA PERVERSIDAD DE LA NATURALEZA. No se puede determinar a priori en que lado de la tostada hay que poner la mantequilla.")
                main.ingestion.ingest(notebook["id"], source["id"])
                indexed = RetrievalService(repository, SQLiteVectorIndex(main.db, main.now), Embeddings(),
                                            ChunkingConfig(target_tokens=100, max_tokens=120))
                asyncio.run(indexed.index_source(notebook["id"], source["id"]))
                main.retrieval_service = lambda _client: indexed
                main.agent_profiles = AgentProfileService(AgentProfileRepository(main.db, main.now), main.module_registry.tool_catalog)
                main.agent_profile_resolver = main.AgentProfileResolver(main.agent_profiles.repository)
                profile = main.agent_profiles.create(AgentProfileInput(
                    name="Tiel", provider_id="p", model_id="Cyber-Tiel", system_instructions="You are Tiel. Preserve this Soul."))
                research = main.agent_profiles.create(AgentProfileInput(
                    name="Research", provider_id="p", model_id="Cyber-Tiel", system_instructions="Research from its Notebook.",
                    notebook_ids=(notebook["id"],), max_tool_calls=12))

                no_agent_no_notebook = self.run_chat(main, "one", "chat", None, None)
                tiel_no_notebook = self.run_chat(main, "two", "agent", profile["id"], None)
                no_agent_notebook = self.run_chat(main, "three", "chat", None, notebook["id"])
                tiel_notebook = self.run_chat(main, "four", "agent", profile["id"], notebook["id"])

                self.assertNotIn("amethyst", no_agent_no_notebook["answer"])
                self.assertNotIn("amethyst", tiel_no_notebook["answer"])
                self.assertIn("amethyst", no_agent_notebook["answer"])
                self.assertIn("amethyst", tiel_notebook["answer"])
                self.assertEqual(no_agent_no_notebook["runtime"]["execution_mode"], "chat")
                self.assertEqual(no_agent_notebook["runtime"]["execution_mode"], "chat")
                self.assertIsNone(no_agent_notebook["runtime"].get("agent_profile_id"))
                self.assertNotIn("You are Tiel", json.dumps(no_agent_notebook["payload"]["messages"]))
                self.assertEqual(no_agent_no_notebook["payload"]["model"], "Raw")
                self.assertEqual(tiel_no_notebook["runtime"]["execution_mode"], "agent")
                self.assertEqual(tiel_notebook["runtime"]["execution_mode"], "agent")
                self.assertEqual(tiel_notebook["runtime"]["agent_profile_name"], "Tiel")
                self.assertEqual(tiel_notebook["runtime"]["resolved_model"], "Cyber-Tiel")
                self.assertEqual(tiel_notebook["runtime"]["resolved_model_name"], "Cyber-Tiel")
                self.assertTrue(tiel_notebook["runtime"]["system_instructions_applied"])
                self.assertEqual(tiel_notebook["runtime"]["notebook_id"], notebook["id"])
                self.assertEqual(tiel_notebook["runtime"]["notebook_request_state"], "value")
                self.assertTrue(tiel_notebook["runtime"]["requested_notebook_id_present"])
                self.assertFalse(tiel_notebook["runtime"]["conversation_notebook_bound"])
                self.assertTrue(tiel_notebook["runtime"]["effective_notebook_bound"])
                self.assertEqual(tiel_notebook["runtime"]["notebook_resolution_source"], "request")
                self.assertEqual(tiel_notebook["runtime"]["notebook_resolution_status"], "resolved")
                self.assertEqual(tiel_notebook["runtime"]["knowledge_retrieval_applied"], True)
                self.assertEqual(tiel_notebook["runtime"]["retrieval_count"], 1)
                self.assertEqual(tiel_notebook["runtime"]["retrieval_status"], "applied")
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

                bound_research = self.run_chat(main, "¿Cuál es la ley de perversidad de la naturaleza?", "agent", research["id"], self.UNSET)
                self.assertIn("perversidad de la naturaleza", bound_research["answer"])
                self.assertEqual(bound_research["runtime"]["notebook_resolution_source"], "agent")
                self.assertEqual(bound_research["runtime"]["notebook_id"], notebook["id"])
                self.assertTrue(bound_research["runtime"]["grounding_applied"])
                self.assertEqual(bound_research["citations"][0]["source_id"], source["id"])
                self.assertIn("LEY DE LA PERVERSIDAD DE LA NATURALEZA", bound_research["payload"]["messages"][1]["content"])

                bound_chat = self.run_chat(main, "five", "chat", None, notebook["id"])
                inherited_chat = self.run_chat(main, "six", "chat", None, self.UNSET, bound_chat["conversation_id"])
                self.assertEqual(inherited_chat["runtime"]["notebook_id"], notebook["id"])
                self.assertEqual(inherited_chat["runtime"]["notebook_request_state"], "omitted")
                self.assertFalse(inherited_chat["runtime"]["requested_notebook_id_present"])
                self.assertTrue(inherited_chat["runtime"]["conversation_notebook_bound"])
                self.assertEqual(inherited_chat["runtime"]["notebook_resolution_source"], "conversation")
                self.assertTrue(inherited_chat["runtime"]["grounding_applied"])
                self.assertTrue(any("amethyst" in str(message["content"]) for message in inherited_chat["payload"]["messages"]))
                changed_model = self.run_chat(main, "model switch", "chat", None, self.UNSET, bound_chat["conversation_id"], model_id="Cyber-Tiel")
                self.assertEqual(changed_model["runtime"]["notebook_id"], notebook["id"])

                main.update_conversation(bound_chat["conversation_id"], main.ConversationPatch(execution_mode="agent", agent_profile_id=profile["id"]))
                switched_agent = self.run_chat(main, "seven", "agent", profile["id"], self.UNSET, bound_chat["conversation_id"])
                self.assertEqual(switched_agent["runtime"]["notebook_id"], notebook["id"])
                main.update_conversation(bound_chat["conversation_id"], main.ConversationPatch(execution_mode="chat", agent_profile_id=None))
                switched_chat = self.run_chat(main, "eight", "chat", None, self.UNSET, bound_chat["conversation_id"])
                self.assertEqual(switched_chat["runtime"]["notebook_id"], notebook["id"])

                cleared = self.run_chat(main, "nine", "chat", None, None, bound_chat["conversation_id"])
                self.assertEqual(cleared["runtime"].get("retrieval_reason"), "notebook_not_bound")
                self.assertEqual(cleared["runtime"]["notebook_request_state"], "null")
                self.assertEqual(cleared["runtime"]["notebook_resolution_source"], "cleared")
                self.assertEqual(cleared["runtime"]["notebook_resolution_status"], "not_bound")
                self.assertFalse(cleared["runtime"].get("grounding_applied", False))
                self.assertIsNone(main.get_conversation(bound_chat["conversation_id"])["conversation"]["notebook_id"])
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.notebooks, main.ingestion, main.retrieval_service = old_notebooks, old_ingestion, old_retrieval

    UNSET = object()

    def run_chat(self, main, content, execution_mode, profile_id, notebook_id=UNSET, conversation_id=None, model_id="Raw"):
        Client.payloads = []
        values = {"provider_id": "p", "model_id": model_id, "content": content,
                  "execution_mode": execution_mode, "agent_profile_id": profile_id}
        if conversation_id is not None:
            values["conversation_id"] = conversation_id
        if notebook_id is not self.UNSET:
            values["notebook_id"] = notebook_id
        response = asyncio.run(main.chat(main.ChatIn(**values)))
        body = asyncio.run(self.collect(response.body_iterator))
        packets = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        done = next(packet for packet in packets if packet.get("done"))
        answer = "".join(packet.get("delta", "") for packet in packets)
        return {"answer": answer, "runtime": done["runtime"], "citations": done["citations"], "payload": Client.payloads[0], "conversation_id": done["conversation_id"]}

    async def collect(self, iterator):
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


if __name__ == "__main__":
    unittest.main()
