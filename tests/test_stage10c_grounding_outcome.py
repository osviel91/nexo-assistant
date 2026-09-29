import asyncio
import unittest

from fastapi import FastAPI

from app.agent import AgentRunRequest, AgentRuntime
from app.agent_model import ModelStreamChunk
from app.grounding import GroundedContext, KnowledgeOutcome, cited_results, knowledge_outcome_instruction
from app.kernel import ModuleRegistry, ToolExecutionContext
from app.tools import ExposurePolicy, ToolExecutor


class Adapter:
    model_id = "model"

    def __init__(self):
        self.payloads = []

    async def stream(self, messages, tools, temperature=None):
        self.payloads.append(messages)
        yield ModelStreamChunk(content="El notebook no contiene información relevante.")


class Stage10CGroundingOutcomeTests(unittest.TestCase):
    def test_every_outcome_has_a_generation_contract(self):
        for outcome in KnowledgeOutcome:
            with self.subTest(outcome=outcome):
                self.assertTrue(knowledge_outcome_instruction(outcome))

    def test_outcomes_are_typed_and_no_relevant_instruction_preserves_history(self):
        self.assertEqual(KnowledgeOutcome.NO_RELEVANT_EVIDENCE.value, "no_relevant_evidence")
        instruction = knowledge_outcome_instruction(KnowledgeOutcome.NO_RELEVANT_EVIDENCE)
        self.assertIn("available", instruction)
        self.assertIn("not retract", instruction)

        adapter = Adapter()
        registry = ModuleRegistry(FastAPI())
        tools = ExposurePolicy().resolve(registry.tool_catalog_view(), set())
        request = AgentRunRequest(
            adapter,
            [{"role": "assistant", "content": "La ley es [S1]."}, {"role": "user", "content": "¿Qué dice sobre Kubernetes?"}],
            tools,
            ToolExecutor(),
            ToolExecutionContext("conversation", "provider", "model", 0),
            knowledge_outcome=KnowledgeOutcome.NO_RELEVANT_EVIDENCE,
        )
        asyncio.run(self._collect(request))
        system_text = "\n".join(message["content"] for message in adapter.payloads[0] if message["role"] == "system")
        self.assertIn("no evidence relevant", system_text)
        self.assertIn("previous grounded answers", system_text)
        self.assertNotIn("No Notebook is bound", system_text)

    async def _collect(self, request):
        return [event async for event in AgentRuntime().stream(request)]

    def test_rejected_turn_has_no_grounding_context_or_citation(self):
        context = GroundedContext.build("n", "Kubernetes", [], 1000)
        self.assertEqual(context.context_chars, 0)
        self.assertEqual(cited_results("Previous answer [S1]", context), [])


if __name__ == "__main__":
    unittest.main()
