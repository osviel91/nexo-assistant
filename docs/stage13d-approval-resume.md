# Stage 13D — Human approval and resumable tool execution

Mutating, destructive, and unclassified tool calls pause the current agent run. Read-only calls continue under the existing policy. An approval is for one provider-issued `tool_call_id` and its exact canonical JSON arguments; the API accepts only `approve` or `reject`, never replacement arguments or a session grant.

## Durable lifecycle

`pending_approvals` stores the source conversation/message/run, tool call ID, canonical arguments plus SHA-256, tool identity fingerprint, a deterministic public summary, and a private continuation checkpoint. Statuses are pending, approved, rejected, executing, executed, failed, expired, or invalidated. The summary projects only service, environment, resource, namespace, action, and name; credential-like keys and credential-shaped values are omitted. Raw arguments and tool results are not included in the approval UI or runtime trace metadata.

Approval resolution uses a SQLite immediate transaction and a pending-only compare-and-set. Execution additionally checks argument integrity, tool identity/classification/schema, and membership in the original effective tool snapshot. Execution transitions through `executing` before invoking the handler, so concurrent decisions cannot invoke twice. A process crash while executing is ambiguous: Nexo does not automatically retry that call. This deliberately favors at-most-once behavior over automatic recovery of a possibly completed external side effect.

## Resume semantics

The initial SSE stream closes after persisting the checkpoint and marking the run `waiting_approval`. A decision opens a new SSE stream and resumes the same `runtime_run`, preserving provider/model, runtime/profile snapshot, provider-native assistant message containing the original tool call, exact tool result with the matching `tool_call_id`, tool-call/round budget, and tools already used. Rejection is returned to the provider as a tool result; it does not execute the tool. Later approvals pause and resume the same run in the same way.

`native.request_user_input` remains a separate user-input interaction. It gathers structured answers and is not authorization to execute a tool.

## Conversation lifecycle

Approvals are rendered beneath their source message and are restored from the conversation API after reload. Branching copies messages but does not copy approvals or authorize a pending action in the branch; the original approval remains scoped to its source conversation/run. Deleting the conversation cascades to its approval records. Pending records have no time-based expiry; unresolved approvals remain visible until decided or their source conversation is deleted.

Diagnostics expose safe runtime event metadata and approval status only. The approval endpoint does not return frozen arguments, checkpoints, provider keys, or tool results.
