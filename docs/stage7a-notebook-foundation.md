# Stage 7A: Notebook Foundation

`AgentProfile` describes how an assistant runs. `Notebook` describes persistent knowledge that may later be composed with a conversation. Stage 7A keeps that relationship optional: conversations and agents are unchanged, and no notebook is a chat.

## Source boundary

`NotebookSource` accepts `file` and `web` sources. Its lifecycle is persisted as `added`, `pending`, `extracting`, `ready`, or `failed`; Stage 7A creates sources as `added` and performs no extraction, fetching, chunking, embedding, retrieval, or citation work.

The future ingestion seam is:

```text
SourceAdapter -> CanonicalDocument -> chunks/retrieval (future)
```

File, web, GitHub, Drive, Obsidian, and MCP adapters can later produce the same canonical document shape with provenance such as original URL, repository path, PDF page, heading, or text offsets. The source row keeps the stable notebook/source identity and content hash needed to retain that chain without putting vector-store fields in `Notebook`.

Files belong to notebook sources, not chat attachments. They are stored under `NEXO_DATA_DIR/notebook-sources` with generated names. Deleting a source or notebook deletes owned files; API responses never expose storage paths.

Web sources store only an HTTP(S) URL, title, and metadata. URLs are not fetched. Search results are not automatically persisted.
