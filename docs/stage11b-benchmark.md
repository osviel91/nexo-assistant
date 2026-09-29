# Stage 11B Retrieval Benchmark

The live 20/15/10/8/5 reranker candidate-limit experiment measured:

| Candidate limit | Reranker p50 (ms) | Retrieval p50 (ms) |
| ---: | ---: | ---: |
| 20 | 2378.56 | 2834.78 |
| 15 | 1848.62 | 2278.41 |
| 10 | 1182.29 | 1643.44 |
| 8 | 964.85 | 1417.52 |
| 5 | 610.88 | 1052.30 |

Limit 8 is the smallest tested value preserving the control's measured quality,
expected `KnowledgeOutcome`, and provenance. Limit 5 is faster but reduces
precision/nDCG and loses expected provenance. The default reranker candidate
limit is therefore 8; other retrieval settings and the reranker model are
unchanged.

The new default applies when configuration is first created or the field is
absent. Existing persisted values are read as stored: an explicit value of 20
is not rewritten or silently replaced.
