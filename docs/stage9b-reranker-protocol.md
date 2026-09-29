# Stage 9B Reranker Protocol

Nexo's current `OpenAICompatibleReranker` uses a generic HTTP reranking
protocol. It is intentionally not an oMLX adapter and does not download or
select a model.

## Endpoint

`POST {provider.base_url}/rerank`

The configured provider's base URL is used as-is after trimming a trailing
slash. The request uses `Content-Type: application/json` and, when configured,
the provider's server-side bearer credential.

## Request

```json
{
  "model": "<configured reranker model>",
  "query": "<user query>",
  "documents": ["<candidate text>", "<candidate text>"],
  "top_n": 20
}
```

The adapter sends candidate text only to the provider. Chunk IDs, offsets,
provenance, credentials, and raw provider data are not sent as diagnostics.

## Response

```json
{
  "results": [
    {"index": 1, "relevance_score": 0.98},
    {"index": 0, "relevance_score": 0.12}
  ]
}
```

`index` refers to the request's zero-based `documents` array. Results must be
unique, valid, scored numerically, and complete for the requested `top_n`.
Malformed, empty, partial, unavailable, or timed-out responses fail open to
RRF.

This protocol is the Stage 9B compatibility target. oMLX integration remains
out of scope until its supported reranking protocol is established.
