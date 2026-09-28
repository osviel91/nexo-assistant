# Stage 7D: Grounded Notebook Chat

Notebook grounding is an optional conversation binding. An omitted `notebook_id` inherits the stored conversation binding; an explicit ID selects and validates a notebook; explicit `null` clears it. Notebook deletion clears bindings but leaves conversations and messages intact.

`GroundedContext` is created per run from the exact current user message. It contains the notebook ID, query, ranked retrieval results, character usage, and truncation state. Retrieval runs before `AgentRuntime`, is not a tool, and is bounded by `NEXO_RAG_TOP_K` (default 5) and `NEXO_RAG_MAX_CONTEXT_CHARS` (default 12000).

The agent profile system message remains separate from grounding instructions and the delimited, explicitly untrusted source material. Source excerpts receive stable per-run IDs such as `[S1]`; server-side parsing accepts only IDs present in that context and deduplicates them.

Assistant citations are stored in `message_citations` with source, document, chunk, offsets, provenance, and content hashes, not copied content. On reload, citations are marked `valid`, `changed`, or `unavailable`. Re-ingestion that changes content, re-indexing that removes a cited chunk, or source deletion therefore cannot silently make an old answer point at new material. The trade-off is that historical citations can become unavailable instead of remaining clickable after deletion.

Runtime traces expose only notebook ID, counts, durations, context size, truncation, and citation count. They never store queries, source contents, prompts, completions, or hidden reasoning.
