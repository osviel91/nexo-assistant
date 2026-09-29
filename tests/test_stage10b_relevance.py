import unittest

from app.grounding import GroundedContext
from app.retrieval import RetrievalCandidate, apply_relevance_gate


def candidate(chunk_id, text):
    return RetrievalCandidate(chunk_id, "source", "document", text, [], 0, len(text))


class Stage10BRelevanceTests(unittest.TestCase):
    def test_positive_and_out_of_domain_candidates_are_separated(self):
        results = [candidate(str(index), "generic page") for index in range(4)]
        results.append(candidate("page-38", "LEY DE LA PERVERSIDAD DE LA NATURALEZA. La tostada cae con mantequilla."))

        relevant = apply_relevance_gate(results, ("¿Cuál es la ley de perversidad de la naturaleza?",), 1)

        self.assertEqual(relevant, 1)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[-1].relevance_reason, "term_overlap")
        self.assertEqual([item.chunk_id for item in results if item.relevant], ["page-38"])

        context = GroundedContext.build("n", "¿Cuál es la ley de perversidad de la naturaleza?", [item.as_dict() for item in results if item.relevant], 1000)
        self.assertEqual(context.retrieval_results[0]["chunk_id"], "page-38")

    def test_conversational_query_keeps_contextual_terms(self):
        results = [candidate("page-38", "LEY DE LA PERVERSIDAD DE LA NATURALEZA. La tostada cae.") ,
                   candidate("jenning", "Corolario de Jenning sobre la tostada.")]
        self.assertEqual(apply_relevance_gate(results, ("ley de perversidad de la naturaleza ¿Y cuál es su corolario sobre la tostada?",), 1), 2)

    def test_out_of_domain_stays_retrieved_but_not_relevant(self):
        results = [candidate(str(index), "Murphy page") for index in range(5)]
        self.assertEqual(apply_relevance_gate(results, ("¿Qué dice el documento sobre Kubernetes?",), 1), 0)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(item.relevance_reason == "insufficient_evidence" for item in results))


if __name__ == "__main__":
    unittest.main()
