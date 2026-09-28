# Stage 6C Agent UX

## Conversation binding

`conversations.agent_profile_id` is nullable. Existing and new conversations with `NULL` use normal Nexo behavior; there is no persisted default Agent.

For `POST /api/chat`:

- An omitted `agent_profile_id` inherits the conversation binding.
- A profile id validates, binds, and uses that profile for the run.
- An explicit `agent_profile_id: null` clears the binding and uses normal Nexo behavior.

The resolved profile is read at run start. Its provider, model, instructions, temperature, and requested tools are therefore an immutable run snapshot. Later edits affect future runs only.

Deleting a profile first clears matching conversation bindings, then deletes the profile. Conversations remain available.

## UI

The Agent selector is shared by Standard and Developer presentations. Standard shows name and description; Developer keeps the same state and compactly exposes the selection in the technical header. The Agents sheet provides list, create, edit, and delete flows, provider/model dropdowns from `/api/providers`, and tool checkboxes from `/api/tools`. Unavailable models and tools remain visible as degraded configuration.

Provider API keys, authorization headers, MCP credentials, system instructions, and tool arguments are not included in the Agent list contract or runtime trace.
