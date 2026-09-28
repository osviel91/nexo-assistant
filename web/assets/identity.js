export function effectiveMessageIdentity(message) {
  const runtime = message?.runtime || {};
  const agent = (runtime.agent_profile_name || 'Nexo').toUpperCase();
  const model = runtime.resolved_model || message?.model_id || '';
  return `${agent} · ${model}`;
}
