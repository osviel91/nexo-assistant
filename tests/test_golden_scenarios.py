import unittest

from app.grounding import GroundedContext, KnowledgeOutcome, cited_results
from app.retrieval import RetrievalCandidate, apply_relevance_gate


def candidate(chunk_id, content, provenance=None):
    return RetrievalCandidate(chunk_id, "murphy-source", "murphy-doc", content,
                               provenance or [], 0, len(content))


class GoldenNexoScenarioTests(unittest.TestCase):
    def test_direct_query_has_relevant_grounded_cited_provenance(self):
        results = [candidate("law", "LEY DE LA PERVERSIDAD DE LA NATURALEZA: la tostada cae del lado de la mantequilla.",
                             [{"source_type": "pdf", "source_location": {"page": 38}}])]
        self.assertEqual(apply_relevance_gate(results, ("¿Cuál es la ley de perversidad de la naturaleza?",), 1), 1)
        context = GroundedContext.build("murphy", "law", [results[0].as_dict()], 1000)
        self.assertEqual(KnowledgeOutcome.GROUNDING_APPLIED.value, "grounding_applied")
        self.assertEqual(cited_results("La respuesta es [S1].", context)[0][1]["provenance"][0]["source_location"]["page"], 38)

    def test_conversational_corollary_resolves_against_previous_turn(self):
        results = [candidate("jenning", "COROLARIO DE JENNING: la tostada cae siempre sobre el lado con mantequilla.")]
        query = ("¿Cuál es la ley de perversidad de la naturaleza?", "¿Y cuál es su corolario sobre la tostada?")
        self.assertEqual(apply_relevance_gate(results, query, 1), 1)
        self.assertIn("jenning", results[0].text.lower())

    def test_ood_query_has_no_relevant_evidence_or_grounding(self):
        results = [candidate("law", "LEY DE LA PERVERSIDAD DE LA NATURALEZA: la tostada cae.")]
        self.assertEqual(apply_relevance_gate(results, ("¿Qué dice el documento sobre Kubernetes?",), 1), 0)
        context = GroundedContext.build("murphy", "Kubernetes", [], 1000)
        self.assertEqual(context.context_chars, 0)
        self.assertEqual(cited_results("No hay evidencia [S1].", context), [])
        self.assertEqual(KnowledgeOutcome.NO_RELEVANT_EVIDENCE.value, "no_relevant_evidence")


if __name__ == "__main__":
    unittest.main()
