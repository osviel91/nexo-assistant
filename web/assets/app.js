import { renderMarkdown } from './markdown.js';
import { effectiveMessageIdentity } from './identity.js';
import { eligibleRerankerModels, rerankerState } from './reranking-state.js';

const $ = (selector) => document.querySelector(selector);
const state = { providers: [], knowledge: null, agents: [], preferences: {}, conversations: [], notebooks: [], notebookSources: [], currentNotebookId: null, executionMode: 'chat', webEnabled: false, toolsEnabled: false, modules: [], tools: [], shadow: [], traces: [], runs: [], labTab: 'models', conversationId: null, agentProfileId: null, messages: [], attachments: [], busy: false, lastRuntime: null, activity: null, lifecycle: 'Complete', followingBottom: true, streamTimestamps: {}, chatThinking: { enabled: false, budget: null } };
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
function applyPreferences(persist = false) {
  const ui = document.documentElement.dataset.ui || 'standard';
  const scheme = document.documentElement.dataset.colorScheme || 'system';
  document.documentElement.dataset.ui = ui;
  document.documentElement.dataset.colorScheme = scheme;
  $('#prompt').placeholder = ui === 'developer' ? '> Ask anything...' : 'Pregunta lo que quieras...';
  document.querySelectorAll('input[name="ui-style"]').forEach((input) => { input.checked = input.value === ui; });
  document.querySelectorAll('input[name="color-scheme"]').forEach((input) => { input.checked = input.value === scheme; });
  if (persist) {
    try { localStorage.setItem('nexo-ui-style', ui); localStorage.setItem('nexo-color-scheme', scheme); } catch { /* local-only preference is best effort */ }
  }
}

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' };
  const response = await fetch(`/api${path}`, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  if (!response.ok) {
    let detail = await response.text();
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain error */ }
    throw Error(typeof detail === 'string' ? detail : 'La operación no pudo completarse.');
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 2600);
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall back for insecure contexts and denied permissions */ }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed'; textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { copied = false; }
  textarea.remove();
  return copied;
}

function diagnosticValue(value) {
  return value == null || value === '' ? 'unknown' : String(value);
}

function formatRunDiagnostics(runtime = {}) {
  const metrics = runtime.metrics || runtime;
  const value = (key, fallback = runtime[key]) => diagnosticValue(metrics[key] ?? fallback);
  const mode = runtime.agent_profile_id || runtime.execution_mode === 'agent' ? 'AGENT' : 'CHAT';
  const reranker = runtime.reranker_status || (runtime.reranker_provider_id && runtime.reranker_model ? `${runtime.reranker_provider_id} / ${runtime.reranker_model}` : 'not configured');
  return [
    'NEXO Run Diagnostics',
    `Mode: ${mode}`,
    `Agent: ${diagnosticValue(runtime.agent_profile_name)}`,
    `Provider: ${diagnosticValue(runtime.resolved_provider_name || runtime.resolved_provider)}`,
    `Model: ${diagnosticValue(runtime.resolved_model_name || runtime.resolved_model)}`,
    '',
    'Knowledge:',
    `Notebook: ${diagnosticValue(runtime.notebook_name)}`,
    `Available: ${runtime.knowledge_available == null ? 'unknown' : Boolean(runtime.knowledge_available)}`,
    `Outcome: ${diagnosticValue(runtime.knowledge_outcome)}`,
    `Retrieval: ${diagnosticValue(runtime.retrieval_status || (runtime.knowledge_retrieval_applied ? 'applied' : 'not applied'))}`,
    `Mode: ${diagnosticValue(runtime.effective_retrieval_mode || runtime.retrieval_mode)}`,
    `Vector store: ${diagnosticValue(runtime.vector_store)}`,
    `Query analysis: ${runtime.query_analysis_ms == null ? 'unknown' : `${runtime.query_analysis_ms} ms`}`,
    `Query variants: ${value('query_variant_count')}`,
    `Embedding: ${runtime['emb' + 'edding_ms'] == null ? 'unknown' : `${runtime['emb' + 'edding_ms']} ms`}`,
    `Dense search: ${runtime.dense_search_ms == null ? 'unknown' : `${runtime.dense_search_ms} ms`}`,
    `Lexical search: ${runtime.lexical_search_ms == null ? 'unknown' : `${runtime.lexical_search_ms} ms`}`,
    `Fusion: ${runtime.fusion_ms == null ? 'unknown' : `${runtime.fusion_ms} ms`}`,
    `Dense candidates: ${value('dense_candidate_count')}`,
    `Lexical candidates: ${value('lexical_candidate_count')}`,
    `Fused candidates: ${value('fused_candidate_count')}`,
    `Reranker: ${reranker}`,
    `Reranker status: ${diagnosticValue(runtime.reranker_status)}`,
    `Reranker input candidates: ${value('reranker_candidate_count')}`,
    `Reranked candidates: ${value('reranked_candidate_count')}`,
    `Rerank duration: ${runtime.rerank_duration_ms == null ? 'unknown' : `${runtime.rerank_duration_ms} ms`}`,
    `Retrieved: ${value('retrieved_candidate_count', runtime.retrieval_count)}`,
    `Relevant: ${value('relevant_candidate_count')}`,
    `Relevance gate: ${diagnosticValue(runtime.relevance_gate_status)}`,
    `Relevance gate duration: ${runtime.relevance_gate_ms == null ? 'unknown' : `${runtime.relevance_gate_ms} ms`}`,
    `Grounding chunks: ${value('grounding_chunks')}`,
    `Context chars: ${value('context_chars', runtime.grounding_context_chars)}`,
    `Grounded context: ${runtime.grounded_context_ms == null ? 'unknown' : `${runtime.grounded_context_ms} ms`}`,
    `Retrieval total: ${runtime.retrieval_total_ms == null ? 'unknown' : `${runtime.retrieval_total_ms} ms`}`,
    `Cited sources: ${value('citation_count')}`,
    '',
    'Generation:',
    `Request to first token: ${metrics.request_to_first_token_ms == null ? 'unknown' : `${metrics.request_to_first_token_ms} ms`}`,
    `Provider TTFT: ${metrics.provider_ttft_ms == null ? 'unknown' : `${metrics.provider_ttft_ms} ms`}`,
    `Generation: ${metrics.generation_ms == null ? 'unknown' : `${metrics.generation_ms} ms`}`,
    `Total request: ${metrics.total_request_ms == null ? 'unknown' : `${metrics.total_request_ms} ms`}`,
  ].join('\n');
}

function moduleEnabled(id) { return state.modules.some((module) => module.id === id); }
function allModels() { return state.providers.flatMap((provider) => provider.models.map((model) => ({ provider, model }))); }
async function savePreference(key, value) { state.preferences[key] = value; try { await api('/preferences', { method: 'PATCH', body: JSON.stringify({ [key]: value }) }); } catch (error) { toast(error.message); } }
function selected() {
  const [providerId, ...modelParts] = $('#model-select').value.split('::');
  return { provider: state.providers.find((provider) => provider.id === providerId), model: modelParts.join('::') };
}

function renderSelect(keep = true) {
  const select = $('#model-select');
  const oldValue = keep ? select.value : '';
  select.innerHTML = state.providers.length
    ? state.providers.map((provider) => `<optgroup label="${escapeHtml(provider.name)}">${provider.models.map((model) => `<option value="${escapeHtml(provider.id)}::${escapeHtml(model.id)}">${escapeHtml(model.id)}</option>`).join('') || '<option disabled>Sin modelos</option>'}</optgroup>`).join('')
    : '<option value="">Añade un proveedor</option>';
   const preferred = oldValue || state.preferences.last_chat_model;
   if (allModels().some(({ provider, model }) => `${provider.id}::${model.id}` === preferred)) select.value = preferred;
  updateComposerModel();
}

function renderNotebookPicker() {
  const select = $('#notebook-picker');
  if (!select) return;
   select.innerHTML = `<option value="">Knowledge</option>${state.notebooks.map((notebook) => `<option value="${escapeHtml(notebook.id)}">${escapeHtml(notebook.name)} · ${notebook.source_count} sources</option>`).join('')}`;
  select.value = state.currentNotebookId || '';
}

function updateComposerModel() {
  // The active model is controlled from the header only.
}

async function loadProviders() {
  state.providers = await api('/providers');
  renderSelect();
  renderProviderList();
  renderLab();
}
async function loadPreferences() {
  try {
    state.preferences = await api('/preferences');
  } catch {
    // Preferences are optional; they must not prevent the main chat from loading.
    state.preferences = {};
  }
}

async function loadKnowledge() {
  state.knowledge = await api('/settings/embeddings');
  const config = state.knowledge.configuration;
  const providers = (state.knowledge.providers || state.providers).map((provider) => ({ ...provider, models: provider.models.filter((model) => (model.capabilities || []).includes('embedding')) }));
  $('#embedding-provider').innerHTML = providers.map((provider) => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.name)}</option>`).join('');
  const empty = $('#embedding-empty');
  const renderModels = () => {
    const provider = providers.find((item) => item.id === $('#embedding-provider').value);
    const models = provider?.models || [];
    $('#embedding-model').innerHTML = models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.id)}</option>`).join('');
    $('#embedding-model').disabled = !models.length;
    empty.textContent = provider ? `No embedding-capable models configured for ${provider.name}` : 'No embedding-capable models configured. Configure one in Providers & Models.';
    empty.hidden = Boolean(models.length);
  };
  $('#embedding-provider').onchange = renderModels;
  $('#embedding-provider').disabled = !providers.length;
  if (config && providers.some((provider) => provider.id === config.provider_id)) $('#embedding-provider').value = config.provider_id;
  renderModels();
  if (config && [...$('#embedding-model').options].some((option) => option.value === config.model_id)) $('#embedding-model').value = config.model_id;
  const fields = { retrieval_mode: 'embedding-mode', dense_candidate_limit: 'embedding-dense-candidates', lexical_candidate_limit: 'embedding-lexical-candidates', rrf_k: 'embedding-rrf-k', final_top_k: 'embedding-final-top-k', target_chunk_size: 'embedding-target', max_chunk_size: 'embedding-max', overlap: 'embedding-overlap', batch_size: 'embedding-batch', retrieval_max_context_chars: 'embedding-context' };
  if (config) Object.entries(fields).forEach(([key, id]) => { $(`#${id}`).value = config[key]; });
  const rerankerProviders = state.knowledge.providers || state.providers;
  const eligibleProviders = rerankerProviders.filter((provider) => eligibleRerankerModels(rerankerProviders, provider.id).length);
  $('#reranker-provider').innerHTML = eligibleProviders.map((provider) => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.name)}</option>`).join('');
  const persistedProvider = eligibleProviders.some((provider) => provider.id === config?.reranker_provider_id) ? config.reranker_provider_id : eligibleProviders[0]?.id || '';
  $('#reranker-provider').value = persistedProvider;
  $('#reranking-enabled').checked = Boolean(config?.reranking_enabled);
  const renderRerankerModels = (selectedModel = '') => {
    const models = eligibleRerankerModels(rerankerProviders, $('#reranker-provider').value);
    $('#reranker-model').innerHTML = models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.id)}</option>`).join('');
    $('#reranker-model').value = models.some((model) => model.id === selectedModel) ? selectedModel : '';
    const current = rerankerState(rerankerProviders, $('#reranker-provider').value, $('#reranker-model').value, $('#reranking-enabled').checked);
    $('#reranker-provider').disabled = !current.capabilityAvailable;
    $('#reranker-model').disabled = current.modelDisabled;
    $('#reranker-empty').hidden = current.capabilityAvailable;
    $('#reranking-enabled').disabled = !current.capabilityAvailable;
    $('#test-reranker').disabled = !current.configured;
    return current;
  };
  $('#reranker-provider').onchange = () => renderRerankerModels();
  const rerankerView = renderRerankerModels(config?.reranker_model || '');
  $('#reranking-enabled').checked = rerankerView.enabled;
  $('#reranker-candidates').value = config?.reranker_candidate_limit ?? 20;
  $('#reranker-timeout').value = config?.reranker_timeout_ms ?? 3000;
  $('#reranking-enabled').onchange = () => { const current = rerankerState(rerankerProviders, $('#reranker-provider').value, $('#reranker-model').value, $('#reranking-enabled').checked); $('#reranking-enabled').checked = current.enabled; $('#test-reranker').disabled = !current.configured; };
  $('#reranking-fields').hidden = false;
  $('#test-reranker').onclick = async () => { const result = $('#reranker-test-result'); try { result.textContent = 'Testing…'; const data = await api('/settings/embeddings/test-reranker', { method: 'POST', body: JSON.stringify({ query: 'Rank the document most relevant to the query.' }) }); result.textContent = `${data.provider} / ${data.model} · ${data.ready ? 'Ready' : 'Unavailable'} · ${data.documents_ranked} documents · ${data.latency_ms} ms`; } catch (error) { result.textContent = 'Reranker test failed'; } };
  updateRetrievalControls();
  $('#knowledge-health').textContent = `${state.knowledge.health.ready} ready · ${state.knowledge.health.outdated} outdated · ${state.knowledge.health.failed} failed`;
}

function updateRetrievalControls() {
  const mode = $('#embedding-mode').value;
  $('#embedding-dense-candidates').disabled = mode === 'lexical';
  $('#embedding-lexical-candidates').disabled = mode === 'dense';
  $('#embedding-rrf-k').disabled = mode !== 'hybrid';
  $('#embedding-mode').closest('form').classList.toggle('retrieval-hybrid', mode === 'hybrid');
}

async function loadTools() {
  state.tools = await api('/tools');
  renderLab();
}

async function loadAgents() { state.agents = await api('/agents'); if (!state.conversationId && !state.agentProfileId && state.preferences.last_agent_profile) state.agentProfileId = state.preferences.last_agent_profile; renderAgentPicker(); renderAgentList(); }

async function loadNotebooks() { state.notebooks = await api('/notebooks'); renderNotebookPicker(); renderNotebooks(); const benchmarkNotebook = $('#benchmark-notebook'); if (benchmarkNotebook) benchmarkNotebook.innerHTML = state.notebooks.map((notebook) => `<option value="${escapeHtml(notebook.id)}">${escapeHtml(notebook.name)}</option>`).join(''); }
function renderNotebooks() {
  $('#notebook-list').innerHTML = state.notebooks.map((notebook) => `<article class="notebook-card" data-notebook-id="${escapeHtml(notebook.id)}"><button class="notebook-card-main"><strong>${escapeHtml(notebook.name)}</strong><span>${notebook.source_count} source${notebook.source_count === 1 ? '' : 's'}</span><small>${escapeHtml(notebook.description || 'Persistent knowledge space')}</small></button><div class="card-actions"><button data-notebook-edit="${escapeHtml(notebook.id)}">Edit</button><button data-notebook-delete="${escapeHtml(notebook.id)}">Delete</button></div></article>`).join('') || '<div class="empty-providers">No hay notebooks todavía. Crea uno para guardar fuentes.</div>';
  $('#notebook-list').querySelectorAll('.notebook-card-main').forEach((button) => { button.onclick = () => openNotebook(button.closest('[data-notebook-id]').dataset.notebookId); });
  $('#notebook-list').querySelectorAll('[data-notebook-edit]').forEach((button) => { button.onclick = () => editNotebook(button.dataset.notebookEdit); });
  $('#notebook-list').querySelectorAll('[data-notebook-delete]').forEach((button) => { button.onclick = () => deleteNotebook(button.dataset.notebookDelete); });
}
function editNotebook(id = '') {
  const notebook = state.notebooks.find((item) => item.id === id);
  $('#notebook-form').hidden = false; $('#new-notebook').hidden = true;
  $('#notebook-id').value = notebook?.id || ''; $('#notebook-name').value = notebook?.name || ''; $('#notebook-description').value = notebook?.description || '';
  $('#notebook-form-title').textContent = notebook ? 'Edit notebook' : 'New notebook'; $('#notebook-name').focus();
}
function resetNotebookForm() { $('#notebook-form').hidden = true; $('#new-notebook').hidden = false; }
async function deleteNotebook(id) {
  const notebook = state.notebooks.find((item) => item.id === id);
  if (!notebook || !confirm(`Delete ${notebook.name} and its sources?`)) return;
  try { await api(`/notebooks/${id}`, { method: 'DELETE' }); if (state.currentNotebookId === id) { state.currentNotebookId = null; $('#notebook-detail').hidden = true; } await loadNotebooks(); toast('Notebook deleted'); } catch (error) { toast(error.message); }
}
async function openNotebook(id) {
  try { const [notebook, sources] = await Promise.all([api(`/notebooks/${id}`), api(`/notebooks/${id}/sources`)]); state.currentNotebookId = id; state.notebookSources = sources; $('#notebook-detail').hidden = false; renderNotebookDetail(notebook); sources.filter((source) => source.indexing_status === 'indexing').forEach((source) => setIndexingRow(source.id, true)); sources.filter((source) => source.indexing_status === 'ready').forEach((source) => { const button = $('#notebook-detail').querySelector(`[data-source-index="${CSS.escape(source.id)}"]`); if (button) button.textContent = 'Re-index'; }); } catch (error) { toast(error.message); }
}
function setIndexingRow(sourceId, indexing) {
  const article = $('#notebook-detail').querySelector(`[data-source-delete="${CSS.escape(sourceId)}"]`)?.closest('.notebook-source');
  if (!article) return;
  let button = article.querySelector('[data-source-index]');
  if (indexing && !button) {
    button = document.createElement('button');
    button.className = 'text-button';
    button.dataset.sourceIndex = sourceId;
    article.lastElementChild.prepend(button);
    button.onclick = async () => { setIndexingRow(sourceId, true); try { await api(`/notebooks/${state.currentNotebookId}/sources/${sourceId}/index`, { method: 'POST' }); await openNotebook(state.currentNotebookId); } catch (error) { await openNotebook(state.currentNotebookId); toast(error.message); } };
  }
  if (indexing && button) { button.disabled = true; button.innerHTML = '<span class="index-spinner" aria-hidden="true"></span> Indexing…'; }
  const small = article.querySelector('small');
  if (indexing && small && !small.querySelector('.index-progress')) { small.append(' · '); const progress = document.createElement('span'); progress.className = 'index-progress'; progress.textContent = 'Indexing…'; small.append(progress); }
}
function renderNotebookDetail(notebook) {
  const status = { added: ['○ Added', 'status-disabled'], pending: ['○ Pending', 'status-disabled'], extracting: ['◌ Processing', 'status-connected'], ready: ['✓ Ready', 'status-ready'], failed: ['⚠ Extraction failed', 'status-error'] };
  $('#notebook-detail').innerHTML = `<button class="text-button notebook-back" id="notebook-back">← Notebooks</button><div class="notebook-detail-head"><div><span class="eyebrow">NOTEBOOK</span><h3>${escapeHtml(notebook.name)}</h3><p>${escapeHtml(notebook.description || 'Canonical documents are ready for future knowledge processing.')}</p></div><span class="status status-ready">INGESTION AVAILABLE</span></div><div class="notebook-sources-head"><h4>Sources</h4><span>${state.notebookSources.length}</span></div><div class="notebook-source-list">${state.notebookSources.map((source) => { const [label, className] = status[source.status] || [source.status, 'status-disabled']; const action = ['added', 'failed'].includes(source.status) ? `<button class="text-button" data-source-ingest="${escapeHtml(source.id)}">${source.status === 'failed' ? 'Retry' : 'Process'}</button>` : ''; const indexAction = source.status === 'ready' && source.indexing_status !== 'indexing' ? `<button class="text-button" data-source-index="${escapeHtml(source.id)}">${source.indexing_status === 'failed' ? 'Retry index' : 'Index'}</button>` : ''; const indexLabel = source.indexing_status === 'ready' ? `✓ Indexed · ${source.chunk_count || 0} chunks` : source.indexing_status === 'failed' ? '⚠ Index failed' : ''; return `<article class="notebook-source"><div><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(source.type)} · <span class="status ${className}">${label}</span>${source.metadata?.mime ? ` · ${escapeHtml(source.metadata.mime)}` : ''}${indexLabel ? ` · ${escapeHtml(indexLabel)}` : ''}</small>${source.status === 'failed' && source.error_message ? `<small>${escapeHtml(source.error_message)}</small>` : ''}</div><div>${action}${indexAction}<button data-source-delete="${escapeHtml(source.id)}" aria-label="Remove ${escapeHtml(source.title)}">×</button></div></article>`; }).join('') || '<div class="empty-providers">Añade una fuente para convertirla en un documento canónico.</div>'}</div><div class="source-add"><h4>Add source</h4><form id="notebook-file-form"><label>File<input id="notebook-file" type="file" required accept=".pdf,.md,.txt"></label><button class="primary-button" type="submit">Add file</button></form><form id="notebook-web-form"><label>Web URL<input id="notebook-url" type="url" required placeholder="https://example.com/section"></label><label>Title<input id="notebook-web-title" required placeholder="Source title"></label><button class="text-button" type="submit">Add web source</button></form></div>`;
  $('#notebook-back').onclick = () => { $('#notebook-detail').hidden = true; state.currentNotebookId = null; };
  const ingestSource = async (sourceId) => { try { await api(`/notebooks/${state.currentNotebookId}/sources/${sourceId}/ingest`, { method: 'POST' }); await openNotebook(state.currentNotebookId); await loadNotebooks(); } catch (error) { toast(error.message); } };
  $('#notebook-file-form').onsubmit = async (event) => { event.preventDefault(); const file = $('#notebook-file').files[0]; if (!file) return; const form = new FormData(); form.append('file', file); try { const source = await api(`/notebooks/${state.currentNotebookId}/sources`, { method: 'POST', body: form }); await ingestSource(source.id); toast('File processed'); } catch (error) { toast(error.message); } };
  $('#notebook-web-form').onsubmit = async (event) => { event.preventDefault(); try { const source = await api(`/notebooks/${state.currentNotebookId}/sources`, { method: 'POST', body: JSON.stringify({ type: 'web', title: $('#notebook-web-title').value, url: $('#notebook-url').value }) }); await ingestSource(source.id); } catch (error) { toast(error.message); } };
  $('#notebook-detail').querySelectorAll('[data-source-ingest]').forEach((button) => { button.onclick = () => ingestSource(button.dataset.sourceIngest); });
  $('#notebook-detail').querySelectorAll('[data-source-index]').forEach((button) => { button.onclick = async () => { const sourceId = button.dataset.sourceIndex; setIndexingRow(sourceId, true); try { await api(`/notebooks/${state.currentNotebookId}/sources/${sourceId}/index`, { method: 'POST' }); await openNotebook(state.currentNotebookId); } catch (error) { await openNotebook(state.currentNotebookId); toast(error.message); } }; });
  $('#notebook-detail').querySelectorAll('[data-source-delete]').forEach((button) => { button.onclick = async () => { try { await api(`/notebooks/${state.currentNotebookId}/sources/${button.dataset.sourceDelete}`, { method: 'DELETE' }); await openNotebook(state.currentNotebookId); await loadNotebooks(); } catch (error) { toast(error.message); } }; });
}

function currentAgent() { return state.agents.find((agent) => agent.id === state.agentProfileId); }
function renderAgentPicker() {
  const agent = currentAgent();
  $('#agent-picker-name').textContent = agent?.name || 'Seleccionar agente';
}
function renderExecutionMode() {
  document.querySelectorAll('[data-mode]').forEach((button) => { button.classList.toggle('active', button.dataset.mode === state.executionMode); });
  $('#model-picker').hidden = state.executionMode === 'agent';
  $('#agent-picker').hidden = state.executionMode !== 'agent';
}
async function setExecutionMode(mode) {
  if (mode === state.executionMode) return;
  if (mode === 'agent' && !state.agentProfileId) { openSurface('agents'); toast('Selecciona un Agent Profile'); return; }
  state.executionMode = mode;
  if (mode === 'chat' && $('#model-select').value) await savePreference('last_chat_model', $('#model-select').value);
  if (mode === 'agent' && state.agentProfileId) await savePreference('last_agent_profile', state.agentProfileId);
  if (state.conversationId) {
    try { await api(`/conversations/${state.conversationId}`, { method: 'PATCH', body: JSON.stringify({ execution_mode: mode, agent_profile_id: mode === 'agent' ? state.agentProfileId : null }) }); }
    catch (error) { state.executionMode = mode === 'agent' ? 'chat' : 'agent'; toast(error.message); }
  }
  renderExecutionMode(); renderAgentPicker(); renderChats();
}
function renderAgentList() {
  const items = state.agents;
  const html = items.map((agent) => {
    const stored = Boolean(agent.id);
    const warning = stored && !agent.model_available ? '<span class="status status-degraded">Model unavailable</span>' : stored && agent.unavailable_tools?.length ? `<span class="status status-degraded">${agent.unavailable_tools.length} tool${agent.unavailable_tools.length > 1 ? 's' : ''} unavailable</span>` : '';
    return `<div class="agent-option ${(!state.agentProfileId && !stored) || agent.id === state.agentProfileId ? 'selected' : ''}" data-agent-id="${escapeHtml(agent.id)}" role="button" tabindex="0"><span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.description || '')}</small>${warning}</span>${stored ? `<span class="agent-actions"><span class="agent-tools-count">${agent.tool_names.length} tools</span><button type="button" data-edit-agent="${escapeHtml(agent.id)}">Edit</button><button type="button" data-delete-agent="${escapeHtml(agent.id)}">Delete</button></span>` : '<span class="agent-check">✓</span>'}</div>`;
  }).join('') || '<div class="empty-providers">No Agent Profiles available.</div>';
  $('#agent-list').innerHTML = html;
  $('#agent-list').querySelectorAll('[data-agent-id]').forEach((button) => { button.onclick = () => selectAgent(button.dataset.agentId || null); });
  $('#agent-list').querySelectorAll('[data-edit-agent]').forEach((button) => { button.onclick = (event) => { event.stopPropagation(); editAgent(button.dataset.editAgent); }; });
  $('#agent-list').querySelectorAll('[data-delete-agent]').forEach((button) => { button.onclick = async (event) => { event.stopPropagation(); await deleteAgent(button.dataset.deleteAgent); }; });
}
async function selectAgent(id) {
  state.agentProfileId = id || null;
  if (id) await savePreference('last_agent_profile', id);
  if (id) state.executionMode = 'agent';
  renderAgentPicker(); renderAgentList();
  renderExecutionMode();
  if (state.conversationId) try { await api(`/conversations/${state.conversationId}`, { method: 'PATCH', body: JSON.stringify({ execution_mode: 'agent', agent_profile_id: state.agentProfileId }) }); await loadChats(); } catch (error) { toast(error.message); }
  closeSurface();
}
function renderAgentModels(selected = '') {
  $('#agent-model').innerHTML = allModels().map(({ provider, model }) => `<option value="${escapeHtml(provider.id)}::${escapeHtml(model.id)}" ${`${provider.id}::${model.id}` === selected ? 'selected' : ''}>${escapeHtml(provider.name)} / ${escapeHtml(model.id)}</option>`).join('') || '<option value="">Configure a provider first</option>';
}
function renderAgentTools(selected = []) {
  const groups = [['Native', state.tools.filter((tool) => tool.source !== 'mcp')], ['MCP', state.tools.filter((tool) => tool.source === 'mcp')]];
  const known = new Set(state.tools.map((tool) => tool.name));
  const missing = selected.filter((name) => !known.has(name));
  $('#agent-tool-options').innerHTML = groups.map(([title, tools]) => tools.length ? `<div class="agent-tool-group"><strong>${title}</strong>${tools.map((tool) => `<label><input type="checkbox" value="${escapeHtml(tool.name)}" ${selected.includes(tool.name) ? 'checked' : ''}>${escapeHtml(tool.name)}</label>`).join('')}</div>` : '').join('') + (missing.length ? `<div class="agent-tool-group"><strong>Unavailable</strong>${missing.map((name) => `<label class="unavailable-tool"><input type="checkbox" value="${escapeHtml(name)}" checked disabled>⚠ ${escapeHtml(name)} — unavailable</label>`).join('')}</div>` : '') || '<span class="optional">No tools available</span>';
}
function editAgent(id = '') {
  const agent = state.agents.find((item) => item.id === id);
  $('#agent-form').hidden = false; $('#new-agent').hidden = true;
  $('#agent-id').value = agent?.id || ''; $('#agent-name').value = agent?.name || ''; $('#agent-description').value = agent?.description || '';
  renderAgentModels(agent ? `${agent.provider_id}::${agent.model_id}` : ''); $('#agent-instructions').value = agent?.system_instructions || '';
  $('#agent-temperature').value = agent?.model_parameters?.temperature ?? ''; renderAgentTools(agent?.tool_names || []);
  $('#agent-form-title').textContent = agent ? 'Edit agent' : 'New agent';
}
function resetAgentForm() { $('#agent-form').hidden = true; $('#new-agent').hidden = false; }
async function deleteAgent(id) { const agent = state.agents.find((item) => item.id === id); if (!agent || !confirm(`Delete ${agent.name}?`)) return; try { await api(`/agents/${id}`, { method: 'DELETE' }); if (state.agentProfileId === id) state.agentProfileId = null; await loadAgents(); renderAgentPicker(); if (state.conversationId) await openChat(state.conversationId); toast('Agent deleted'); } catch (error) { toast(error.message); } }

async function loadShadow() {
  state.shadow = await api('/lab/shadow?limit=20');
  renderLab();
}

async function loadTraces() {
  [state.traces, state.runs] = await Promise.all([api('/lab/traces?limit=30'), api('/lab/runs?limit=10')]);
  renderLab();
}

async function loadModules() {
  state.modules = await api('/modules');
  const hasTools = moduleEnabled('mcp') || moduleEnabled('web-search-searxng');
  $('#attach-button').hidden = !state.modules.some((module) => module.interface_extensions.some((extension) => extension.id === 'attach-files'));
  $('#web-chip').hidden = !moduleEnabled('web-search-searxng');
  $('#tools-chip').hidden = !hasTools;
  $('#web-chip').setAttribute('aria-pressed', String(state.webEnabled));
  $('#tools-chip').setAttribute('aria-pressed', String(state.toolsEnabled));
  $('#mcp-setting').hidden = !moduleEnabled('mcp');
  $('#decision-setting').hidden = !moduleEnabled('decision-runtime');
  renderLab();
}

async function loadChats() {
  state.conversations = await api('/conversations');
  renderChats();
}

function renderChats() {
  const query = $('#chat-search').value.trim().toLowerCase();
  const chats = state.conversations.filter((chat) => chat.title.toLowerCase().includes(query));
  $('#conversation-list').innerHTML = chats.map((chat) => `<div class="conversation-row"><button class="conversation-item ${chat.id === state.conversationId ? 'selected' : ''}" data-id="${escapeHtml(chat.id)}" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title)}</button><button class="conversation-menu" data-menu-id="${escapeHtml(chat.id)}" aria-label="Manage ${escapeHtml(chat.title)}" aria-expanded="false">⋯</button><div class="conversation-context-menu" data-context-id="${escapeHtml(chat.id)}" hidden><button data-rename-id="${escapeHtml(chat.id)}">Rename</button><button data-delete-id="${escapeHtml(chat.id)}">Delete</button></div></div>`).join('') || '<div class="empty-providers">No hay chats todavía</div>';
  document.querySelectorAll('.conversation-item').forEach((button) => { button.onclick = () => openChat(button.dataset.id); });
  document.querySelectorAll('.conversation-menu').forEach((button) => { button.onclick = (event) => { event.stopPropagation(); const menu = button.parentElement.querySelector('.conversation-context-menu'); menu.hidden = !menu.hidden; button.setAttribute('aria-expanded', String(!menu.hidden)); }; });
  document.querySelectorAll('[data-rename-id]').forEach((button) => { button.onclick = () => renameConversation(button.dataset.renameId); });
  document.querySelectorAll('[data-delete-id]').forEach((button) => { button.onclick = () => deleteConversation(button.dataset.deleteId); });
}
async function renameConversation(id) {
  const chat = state.conversations.find((item) => item.id === id);
  if (!chat) return;
  const title = prompt('New title', chat.title)?.trim();
  if (title) try { await api(`/conversations/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }); await loadChats(); } catch (error) { toast(error.message); }
}
async function deleteConversation(id) {
  const chat = state.conversations.find((item) => item.id === id);
  if (!chat || !confirm(`Delete "${chat.title}"?`)) return;
  try { await api(`/conversations/${id}`, { method: 'DELETE' }); if (state.conversationId === id) beginChat(); await loadChats(); } catch (error) { toast(error.message); }
}

function beginChat() {
  state.conversationId = null;
  state.messages = [];
  state.attachments = [];
  state.agentProfileId = state.preferences.last_agent_profile || null;
  state.executionMode = 'chat';
  state.webEnabled = false;
  state.toolsEnabled = false;
  state.currentNotebookId = null;
  renderNotebookPicker();
  state.lastRuntime = null;
  state.followingBottom = true;
  $('#messages').innerHTML = '';
  $('#welcome').hidden = false;
  renderAttachments();
  renderChats();
  closeSidebar();
  $('#prompt').focus();
}

function isNearBottom() {
  const view = $('#chat-view');
  return view.scrollHeight - view.scrollTop - view.clientHeight < 72;
}

function updateScrollButton() {
  const button = $('#scroll-bottom');
  if (button) button.hidden = isNearBottom();
}

function scrollToBottom(smooth = true) {
  const view = $('#chat-view');
  state.followingBottom = true;
  view.scrollTo({ top: view.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  updateScrollButton();
}

function renderMessages() {
  const box = $('#messages');
  box.innerHTML = state.messages.map((message) => {
    const sources = (message.sources || []).filter((source) => { try { return ['http:', 'https:'].includes(new URL(source.url).protocol); } catch { return false; } });
    const citations = message.citations || [];
    const attachments = (message.attachments || []).map((attachment) => attachment.kind === 'image'
      ? `<img class="attachment-preview" src="${escapeHtml(attachment.data_url)}" alt="${escapeHtml(attachment.name)}">`
      : `<span class="message-model">Adjunto: ${escapeHtml(attachment.name)}</span>`).join('');
    const sourceList = sources.length ? `<details class="message-sources"><summary>Sources · ${sources.length}</summary>${sources.map((source) => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}<span>${escapeHtml(source.url)}</span></a>`).join('')}</details>` : '';
    const notebookList = citations.length ? `<details class="message-sources notebook-sources"><summary>Notebook sources · ${citations.length}</summary>${citations.map((citation) => { const location = citation.provenance?.map((item) => item.source_location || {}).find((item) => item.page != null || item.heading); const suffix = location?.page != null ? ` · page ${location.page}` : location?.heading ? ` · ${location.heading}` : ''; const excerpt = citation.excerpt ? `<details class="citation-excerpt"><summary>Retrieved excerpt</summary><p>${escapeHtml(citation.excerpt)}</p></details>` : ''; return `<div class="notebook-citation"><strong>[${escapeHtml(citation.citation_key)}]</strong> ${escapeHtml(citation.source_title || 'Notebook source')}${escapeHtml(suffix)}<span>${escapeHtml(citation.status || 'Retrieved excerpt')}</span>${excerpt}</div>`; }).join('')}</details>` : '';
    const content = message.role === 'assistant' ? renderMarkdown(message.content) : escapeHtml(message.content).replace(/\n/g, '<br>');
    const runtime = message.runtime || {};
     const metrics = runtime.metrics || runtime;
     const summary = message.role === 'assistant' && (metrics.tokens_per_second != null || metrics.output_tokens != null || metrics.total_duration_ms != null)
       ? `<div class="message-metrics">${metrics.output_tokens != null ? `${escapeHtml(metrics.output_tokens)} tokens` : ''}${metrics.tokens_per_second != null ? ` · ${escapeHtml(metrics.tokens_per_second)} tok/s` : ''}${metrics.total_duration_ms != null ? ` · ${(Number(metrics.total_duration_ms) / 1000).toFixed(2)} s` : ''}</div>` : '';
     const thinking = message.role === 'assistant' && runtime.thinking?.available ? `<details class="thinking-block"><summary>Thinking${runtime.thinking.duration_ms != null ? ` · ${(Number(runtime.thinking.duration_ms) / 1000).toFixed(1)} s` : ''}${runtime.thinking.tokens != null ? ` · ${runtime.thinking.tokens} tokens` : ''}</summary>${runtime.thinking.content ? `<p>${escapeHtml(runtime.thinking.content)}</p>` : ''}${runtime.thinking.budget != null ? `<small>Budget ${escapeHtml(runtime.thinking.budget)}</small>` : ''}</details>` : '';
      const toolbar = message.role === 'assistant' && message.id ? `<div class="message-toolbar"><button class="icon-button" data-copy-message="${escapeHtml(message.id)}" type="button" aria-label="Copy answer" title="Copy answer"><span aria-hidden="true">⧉</span></button><button class="icon-button" data-branch-message="${escapeHtml(message.id)}" type="button" aria-label="Branch from message" title="Branch from message"><span aria-hidden="true">⑂</span></button><button class="icon-button" data-details-message="${escapeHtml(message.id)}" type="button" aria-label="Show run details" title="Show run details"><span aria-hidden="true">ⓘ</span></button></div>` : '';
     const activity = message === state.messages.at(-1) && message.role === 'assistant' && state.activity ? `<div class="message-activity"><span class="activity-dot"></span>${escapeHtml(activityLabel(state.activity))}</div>` : '';
      return `<article class="message ${message.role === 'user' ? 'user' : ''}">${message.role === 'assistant' ? '<div class="avatar-small" aria-hidden="true">n</div>' : ''}<div class="message-body">${message.role === 'assistant' ? `<div class="message-meta">${escapeHtml(effectiveMessageIdentity(message))}</div>` : ''}${thinking}<div class="message-content">${content}</div>${activity}${summary}${toolbar}${attachments}${sourceList}${notebookList}</div></article>`;
   }).join('');
    box.querySelectorAll('[data-copy-message]').forEach((button) => { button.onclick = async () => { const message = state.messages.find((item) => item.id === button.dataset.copyMessage); if (!message) return; const copied = await copyText(message.content || ''); if (copied) { button.querySelector('span').textContent = '✓'; toast('Copied'); setTimeout(() => { if (button.isConnected) button.querySelector('span').textContent = '⧉'; }, 1200); } else toast('Copy failed'); }; });
    box.querySelectorAll('[data-branch-message]').forEach((button) => { button.onclick = () => branchFrom(button.dataset.branchMessage); });
    box.querySelectorAll('[data-details-message]').forEach((button) => { button.onclick = () => { state.lastRuntime = state.messages.find((item) => item.id === button.dataset.detailsMessage)?.runtime || null; $('#config-content').innerHTML = renderConfig(); openSurface('config'); bindConfig(); }; });
   if (state.followingBottom) $('#chat-view').scrollTop = $('#chat-view').scrollHeight;
   updateScrollButton();
}

function activityLabel(activity) {
  if (activity.type === 'RETRIEVE') return 'Consultando el notebook';
  if (activity.type === 'ACT') return `Usando herramienta ${activity.tool || ''}`.trim();
  if (activity.type === 'REASON') return 'Thinking…';
  if (activity.type === 'RESPOND') return 'Responding…';
  return 'Procesando respuesta';
}

function updateStreamingAnswer(answer) {
  const content = $('#messages').querySelector('.message:last-child .message-content');
  if (content) content.innerHTML = renderMarkdown(answer);
  if (state.followingBottom) $('#chat-view').scrollTop = $('#chat-view').scrollHeight;
  updateScrollButton();
}

function updateStreamingActivity(activity) {
  state.activity = activity;
  const message = state.messages.at(-1);
  if (!message || message.role !== 'assistant') return;
  const body = $('#messages').querySelector('.message:last-child .message-body');
  if (!body) return;
  let element = body.querySelector('.message-activity');
  if (!element) { element = document.createElement('div'); element.className = 'message-activity'; body.append(element); }
  element.innerHTML = `<span class="activity-dot"></span>${escapeHtml(activityLabel(activity))}`;
}

async function openChat(id) {
  try {
    const data = await api(`/conversations/${id}`);
    state.conversationId = id;
    state.executionMode = data.conversation.execution_mode || (data.conversation.agent_profile_id ? 'agent' : 'chat');
    state.agentProfileId = data.conversation.agent_profile_id || null;
    state.currentNotebookId = data.conversation.notebook_id || null;
    state.followingBottom = true;
    renderNotebookPicker();
    renderExecutionMode();
    state.messages = data.messages;
    state.lastRuntime = [...state.messages].reverse().find((message) => message.role === 'assistant')?.runtime || null;
    $('#welcome').hidden = true;
    renderMessages();
    renderChats();
    closeSidebar();
  } catch (error) { toast(error.message); }
}

async function branchFrom(messageId) {
  if (!state.conversationId) return;
  try { const branch = await api(`/conversations/${state.conversationId}/branch/${messageId}`, { method: 'POST' }); await loadChats(); await openChat(branch.id); toast('Branched conversation created'); } catch (error) { toast(error.message); }
}

function renderAttachments() {
  $('#attachment-tray').innerHTML = state.attachments.map((attachment, index) => `<span class="attachment-chip">${escapeHtml(attachment.name)}<button data-index="${index}" aria-label="Quitar ${escapeHtml(attachment.name)}">×</button></span>`).join('');
  document.querySelectorAll('.attachment-chip button').forEach((button) => { button.onclick = () => { state.attachments.splice(Number(button.dataset.index), 1); renderAttachments(); }; });
}

async function send() {
  if (state.busy) return;
  const text = $('#prompt').value.trim();
  const choice = selected();
  const visibleNotebookId = $('#notebook-picker')?.value || null;
  if (visibleNotebookId !== state.currentNotebookId) {
    toast('El Notebook seleccionado no está sincronizado.');
    renderNotebookPicker();
    return;
  }
  const agent = currentAgent();
   const effectiveChoice = state.executionMode === 'agent' && agent ? { provider: state.providers.find((provider) => provider.id === agent.provider_id), model: agent.model_id } : choice;
  if (!text && !state.attachments.length) return;
   if (!effectiveChoice.provider && !state.agentProfileId) { openSurface('settings'); toast('Configura un proveedor y selecciona un modelo'); return; }
  state.busy = true;
  state.streamTimestamps = { request_started: performance.now() };
  state.activity = { type: 'REASON', status: 'running' };
  $('#send-button').disabled = true;
  $('#welcome').hidden = true;
  state.messages.push({ role: 'user', content: text, attachments: state.attachments.slice(), model_id: effectiveChoice.model });
  renderMessages();
  $('#prompt').value = '';
  $('#prompt').style.height = '';
  const attachments = state.attachments.slice();
  state.attachments = [];
  renderAttachments();
  state.messages.push({ role: 'assistant', content: '', model_id: effectiveChoice.model, sources: [] });
  renderMessages();
  let answer = '';
  try {
     const chatPayload = { conversation_id: state.conversationId, provider_id: effectiveChoice.provider?.id || '', model_id: effectiveChoice.model || '', content: text, attachments };
      chatPayload.execution_mode = state.executionMode;
      chatPayload.web_enabled = state.webEnabled;
       chatPayload.tools_enabled = state.toolsEnabled;
       if (state.executionMode === 'chat') chatPayload.thinking = state.chatThinking;
      if (state.executionMode === 'agent') chatPayload.agent_profile_id = state.agentProfileId;
      if (state.currentNotebookId !== null) chatPayload.notebook_id = state.currentNotebookId;
     const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(chatPayload) });
    if (!response.ok) throw Error((await response.text()).slice(0, 300));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      if (done && buffer.trim()) { events.push(buffer); buffer = ''; }
      for (const event of events) {
        const line = event.split('\n').find((item) => item.startsWith('data: '));
        if (!line) continue;
        const data = JSON.parse(line.slice(6));
        if (data.error) throw Error(data.error);
         if (data.status) toast(data.message);
         if (data.activity) { if (data.activity.type === 'REASON') state.streamTimestamps.reasoning_started ||= performance.now(); updateStreamingActivity(data.activity); }
         if (data.delta) { state.streamTimestamps.first_answer_delta ||= performance.now(); state.activity = { type: 'RESPOND', status: 'running' }; answer += data.delta; state.messages.at(-1).content = answer; updateStreamingActivity(state.activity); updateStreamingAnswer(answer); }
       if (data.done) { state.streamTimestamps.completed = performance.now(); state.conversationId = data.conversation_id; state.lastRuntime = data.runtime || null; state.activity = null; Object.assign(state.messages.at(-1), { id: data.message_id, content: answer, provider_id: data.provider_id, model_id: data.model_id, sources: data.sources || [], citations: data.citations || [], runtime: data.runtime || {} }); renderMessages(); }
      }
      if (done) break;
    }
    if (!answer) state.messages.pop();
    await loadChats();
  } catch (error) {
    state.messages.at(-1).content = 'No se pudo completar la respuesta.';
    renderMessages();
    toast(error.message || 'No se pudo completar la respuesta.');
  } finally {
    state.activity = null;
    state.busy = false;
    $('#send-button').disabled = false;
    $('#prompt').focus();
  }
}

function openSurface(name) {
  $('#surface-backdrop').hidden = false;
  $('#lab-surface').hidden = name !== 'lab';
  $('#settings-surface').hidden = name !== 'settings';
  $('#agents-surface').hidden = name !== 'agents';
  $('#notebooks-surface').hidden = name !== 'notebooks';
  $('#config-surface').hidden = name !== 'config';
  if (name === 'settings') { resetForm(); applyPreferences(); renderProviderList(); if (state.notebooks.length) $('#benchmark-notebook').value = state.currentNotebookId || state.notebooks[0].id; $('#provider-name').focus(); }
  if (name === 'agents') { resetAgentForm(); renderAgentList(); }
  if (name === 'notebooks') { resetNotebookForm(); renderNotebooks(); }
}

let benchmarkResult = null;
function renderBenchmarkResult(job) {
  const progress = $('#benchmark-progress');
  progress.hidden = false;
  progress.textContent = job.status === 'running'
    ? `${job.progress.completed}/${job.progress.total} scenarios · ${job.progress.scenario || 'Preparing'} · candidate limit ${job.progress.candidate_limit ?? '—'}`
    : job.status === 'failed' ? `Failed: ${job.error}` : 'Benchmark completed';
  $('#benchmark-status').textContent = job.status;
  $('#benchmark-run').disabled = job.status === 'running';
  if (job.status !== 'completed') return;
  benchmarkResult = job.result;
  const experiments = benchmarkResult.experiments || [];
  const baseline = experiments.find((experiment) => experiment.candidate_limit === 20);
  const scenarios = new Map((baseline?.scenarios || []).map((scenario) => [scenario.name, scenario]));
  const delta = (value, base, digits = 4) => value == null || base == null ? 'n/a' : (value - base).toFixed(digits);
  const comparisons = experiments.flatMap((experiment) => experiment.scenarios.map((scenario) => {
    const control = scenarios.get(scenario.name);
    return { limit: experiment.candidate_limit, ...scenario,
      latencyDelta: delta(scenario.latency_statistics.reranker_duration_ms.p50, control?.latency_statistics.reranker_duration_ms.p50, 2),
      qualityDelta: Object.fromEntries(['recall_at_k', 'precision_at_k', 'mrr', 'ndcg_at_k'].map((key) => [key, delta(scenario.quality_statistics[key], control?.quality_statistics[key])])) };
  }));
  $('#benchmark-results').hidden = false;
  $('#benchmark-results').innerHTML = `<div class="model-row"><strong>Limit / scenario</strong><strong>Reranker p50 Δ ms</strong><strong>Recall Δ</strong><strong>Precision Δ</strong><strong>MRR Δ</strong><strong>nDCG Δ</strong><strong>Invalid</strong></div>${comparisons.map((row) => `<div class="model-row"><strong>${row.candidate_limit} · ${escapeHtml(row.name)}</strong><span>${escapeHtml(row.latencyDelta)}</span><span>${escapeHtml(row.qualityDelta.recall_at_k)}</span><span>${escapeHtml(row.qualityDelta.precision_at_k)}</span><span>${escapeHtml(row.qualityDelta.mrr)}</span><span>${escapeHtml(row.qualityDelta.ndcg_at_k)}</span><span>${row.invalid_runs.length}</span></div>`).join('')}`;
  $('#benchmark-actions').hidden = false;
}

$('#retrieval-benchmark-form').onsubmit = async (event) => {
  event.preventDefault();
  let candidate_limits;
  try {
    candidate_limits = $('#benchmark-candidates').value.split(',').map((value) => Number(value.trim()));
    if (!candidate_limits.length || candidate_limits.length > 10 || candidate_limits.some((value) => !Number.isInteger(value) || value < 1 || value > 200) || new Set(candidate_limits).size !== candidate_limits.length) throw Error('Enter 1-10 unique limits from 1 to 200');
    const repetitions = Number($('#benchmark-repetitions').value);
    if (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 20) throw Error('Repetitions must be from 1 to 20');
    benchmarkResult = null;
    $('#benchmark-results').hidden = true; $('#benchmark-actions').hidden = true;
    const job = await api('/settings/retrieval-benchmark', { method: 'POST', body: JSON.stringify({ notebook_id: $('#benchmark-notebook').value, candidate_limits, repetitions }) });
    renderBenchmarkResult(job);
    while (job.status === 'running') {
      await new Promise((resolve) => setTimeout(resolve, 500));
      Object.assign(job, await api(`/settings/retrieval-benchmark/${encodeURIComponent(job.id)}`));
      renderBenchmarkResult(job);
    }
  } catch (error) { $('#benchmark-status').textContent = error.message; }
};
$('#benchmark-copy').onclick = async () => { if (benchmarkResult && await copyText(JSON.stringify(benchmarkResult, null, 2))) toast('Benchmark copied'); };
$('#benchmark-download').onclick = () => { if (!benchmarkResult) return; const url = URL.createObjectURL(new Blob([`${JSON.stringify(benchmarkResult, null, 2)}\n`], { type: 'application/json' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'retrieval-benchmark.json'; anchor.click(); URL.revokeObjectURL(url); };

function renderConfig() {
  const runtime = state.lastRuntime || {};
  const row = (label, value) => value == null || value === '' ? '' : `<div class="config-value"><span>${label}</span> ${escapeHtml(value)}</div>`;
   const metrics = runtime.metrics || runtime;
    const knowledge = `${row('Notebook', runtime.notebook_name || 'Not bound')}${row('Knowledge', runtime.knowledge_status === 'available' || runtime.knowledge_available ? 'Available' : `Not available${runtime.knowledge_unavailable_reason ? ` (${runtime.knowledge_unavailable_reason})` : ''}`)}${row('Knowledge outcome', runtime.knowledge_outcome)}${row('Retrieval', runtime.retrieval_status || (runtime.knowledge_retrieval_applied ? 'applied' : 'not applied'))}${row('Retrieval reason', runtime.retrieval_reason)}${row('Grounding', runtime.grounding_status || (runtime.grounding_applied ? 'applied' : 'not applied'))}${row('Grounding reason', runtime.grounding_reason)}${row('Retrieval mode', runtime.retrieval_mode)}${row('Dense candidates', runtime.dense_candidate_count)}${row('Lexical candidates', runtime.lexical_candidate_count)}${row('Fused candidates', runtime.fused_candidate_count)}${row('Reranker', runtime.reranker_provider_id && runtime.reranker_model ? `${runtime.reranker_provider_id} / ${runtime.reranker_model}` : null)}${row('Reranker status', runtime.reranker_status)}${row('Reranker reason', runtime.reranker_reason)}${row('Reranked candidates', runtime.reranked_candidate_count)}${row('Rerank duration', runtime.rerank_duration_ms != null ? `${runtime.rerank_duration_ms} ms` : null)}${row('Retrieved candidates', runtime.retrieved_candidate_count ?? runtime.retrieval_count)}${row('Relevant candidates', runtime.relevant_candidate_count)}${row('Relevance gate', runtime.relevance_gate_status)}${row('Relevance reason', runtime.relevance_gate_reason)}${row('Grounding chunks', runtime.grounding_chunks)}${row('Context chars', runtime.context_chars)}${row('Retrieval duration', runtime.retrieval_duration_ms != null ? `${runtime.retrieval_duration_ms} ms` : null)}${row('Cited sources', runtime.citation_count)}`;
    const selectedModel = selected().model;
    const capabilities = selected().provider?.models.find((model) => model.id === selectedModel)?.capabilities || [];
    const thinkingEditor = !runtime.agent_profile_id && capabilities.includes('thinking') ? `<div class="config-block"><h3>THINKING</h3><label class="config-toggle"><input id="thinking-enabled" type="checkbox" ${state.chatThinking.enabled ? 'checked' : ''}> Enable supported thinking</label>${capabilities.includes('thinking-budget') ? `<label class="config-value">Budget <input id="thinking-budget" type="number" min="1" step="1" value="${escapeHtml(state.chatThinking.budget ?? '')}"></label>` : ''}<small class="config-note">Only adapter-declared capabilities are shown.</small></div>` : '';
    return `${state.lastRuntime ? '' : '<div class="lab-empty">No completed run yet. Current selection will be used for the next run.</div>'}<div class="config-block"><h3>EFFECTIVE RUN</h3>${row('Mode', runtime.agent_profile_id || state.executionMode === 'agent' ? 'AGENT' : 'CHAT')}${row('Agent', runtime.agent_profile_name || currentAgent()?.name)}${row('Provider', runtime.resolved_provider_name || runtime.resolved_provider || (state.executionMode === 'agent' ? currentAgent()?.provider_id : selected().provider?.name))}${row('Model', runtime.resolved_model_name || runtime.resolved_model || (state.executionMode === 'agent' ? currentAgent()?.model_id : selected().model))}${row('Soul / instructions', runtime.system_instructions_applied ? 'Applied' : state.executionMode === 'agent' ? 'Agent controlled' : 'Not applied')}${row('Parameters', [runtime.temperature, runtime.top_p, runtime.top_k].filter((value) => value != null).join(' · '))}${row('Tools', (runtime.effective_tool_names || []).length || state.executionMode === 'agent' ? 'Agent controlled' : 'none')}</div>${thinkingEditor}<div class="config-block"><h3>KNOWLEDGE</h3>${knowledge}</div><div class="config-block"><h3>GENERATION METRICS</h3>${row('Input / output', metrics.input_tokens != null || metrics.output_tokens != null ? `${metrics.input_tokens ?? 'unknown'} / ${metrics.output_tokens ?? 'unknown'}` : null)}${row('Context utilization', metrics.context_utilization)}${row('Request to first token ms', metrics.request_to_first_token_ms)}${row('Provider TTFT ms', metrics.provider_ttft_ms)}${row('Generation ms', metrics.generation_ms)}${row('Total request ms', metrics.total_request_ms)}${row('Tokens/sec', metrics.tokens_per_second)}</div>`;
}
function bindConfig() {
  $('#thinking-enabled')?.addEventListener('change', (event) => { state.chatThinking.enabled = event.target.checked; });
  $('#thinking-budget')?.addEventListener('change', (event) => { state.chatThinking.budget = event.target.value ? Number(event.target.value) : null; });
  const copyButton = $('#copy-diagnostics');
  copyButton.hidden = !state.lastRuntime;
  copyButton.onclick = async () => {
    if (!state.lastRuntime) return;
    const copied = await copyText(formatRunDiagnostics(state.lastRuntime));
    if (copied) { copyButton.querySelector('span').textContent = '✓'; toast('Diagnostics copied'); setTimeout(() => { if (copyButton.isConnected) copyButton.querySelector('span').textContent = '⧉'; }, 1200); }
    else toast('Copy failed');
  };
}
function closeSurface() { $('#surface-backdrop').hidden = true; }
function openSidebar() { $('#sidebar').classList.add('open'); $('#sidebar-backdrop').hidden = false; $('#open-sidebar').setAttribute('aria-expanded', 'true'); }
function closeSidebar() { $('#sidebar').classList.remove('open'); $('#sidebar-backdrop').hidden = true; $('#open-sidebar').setAttribute('aria-expanded', 'false'); }

function renderLab() {
  const tabs = [{ id: 'models', label: 'Models' }];
  if (moduleEnabled('decision-runtime')) tabs.push({ id: 'decisions', label: 'Decisions' });
  if (state.modules.find((module) => module.id === 'decision-runtime')?.status?.shadow_enabled) tabs.push({ id: 'shadow', label: 'Shadow' });
  tabs.push({ id: 'runtime', label: 'Runtime' });
  if (state.tools.length || moduleEnabled('mcp')) tabs.push({ id: 'tools', label: 'Tools' });
  if (!tabs.some((tab) => tab.id === state.labTab)) state.labTab = tabs[0].id;
  $('#lab-tabs').innerHTML = tabs.map((tab) => `<button class="lab-tab ${tab.id === state.labTab ? 'active' : ''}" data-lab-tab="${tab.id}">${tab.label}</button>`).join('');
  $('#lab-tabs').querySelectorAll('[data-lab-tab]').forEach((button) => { button.onclick = () => { state.labTab = button.dataset.labTab; renderLab(); }; });
   $('#lab-content').innerHTML = state.labTab === 'models' ? renderModels() : state.labTab === 'tools' ? renderTools() : state.labTab === 'shadow' ? renderShadow() : state.labTab === 'runtime' ? renderRuntime() : renderDecisions();
  if (state.labTab === 'decisions') bindDecisionPlayground();
}

function renderShadow() {
  const observations = state.shadow.map((item) => {
    const answers = item.answers || {};
    const answer = (id) => answers[id] ? `${answers[id].value === true ? 'Yes' : answers[id].value === false ? 'No' : answers[id].value} · ${Math.round((answers[id].confidence || 0) * 100)}%` : 'Unknown';
    const actual = item.execution || {};
    return `<article class="model-provider"><div class="model-provider-head"><div><h4>Decision observation</h4><span>${escapeHtml(item.created_at)} · ${escapeHtml(item.model || 'unknown model')}</span></div><span class="status ${item.error ? 'status-disabled' : 'status-ready'}">● ${item.error ? 'FAILED' : 'RECORDED'}</span></div><div class="model-list"><div class="model-row"><strong>Web needed</strong><span>${escapeHtml(answer('needs_web'))}</span></div><div class="model-row"><strong>Tools needed</strong><span>${escapeHtml(answer('needs_tools'))}</span></div><div class="model-row"><strong>Task</strong><span>${escapeHtml(answer('task_type'))}</span></div><div class="model-row"><strong>Actual execution</strong><span>${escapeHtml((actual.tools_used || []).join(', ') || 'No tools')} · ${actual.tool_rounds || 0} round(s)</span></div></div></article>`;
  }).join('');
  return `<section class="lab-section"><div class="lab-section-head"><div><span class="eyebrow">SHADOW DECISION</span><h3>Observation history</h3></div><span class="lab-count">${state.shadow.length} observations</span></div>${observations || '<div class="lab-empty">No hay observaciones shadow todavía.</div>'}</section>`;
}

function renderRuntime() {
  const runs = state.runs.map((run) => `<details class="model-provider" ${state.runs[0] === run ? 'open' : ''}><summary class="model-provider-head"><div><h4>RUN · ${escapeHtml(run.status)}</h4><span>${escapeHtml(run.model || 'unknown model')} · ${escapeHtml(run.id)}</span></div><span class="status ${run.status === 'completed' ? 'status-ready' : 'status-disabled'}">${escapeHtml(run.status)}</span></summary><div class="model-list">${state.traces.filter((event) => event.run_id === run.id).map((event) => `<div class="model-row"><strong>${String(event.sequence).padStart(2, '0')} ${escapeHtml(event.type)}</strong><span>${escapeHtml(event.metadata?.tool || event.metadata?.model || event.type)} · ${escapeHtml(String(event.duration_ms ?? 0))}ms</span></div>`).join('') || '<div class="lab-empty">No events</div>'}</div></details>`).join('');
  const events = state.traces.filter((event) => !event.run_id).map((event) => {
    const metadata = event.metadata || {};
    const label = event.type === 'ACT' ? metadata.tool || 'tool' : event.type === 'REASON' ? `${metadata.model || 'model'} · round ${metadata.round || '?'}` : metadata.model || 'shadow decision';
    const details = event.type === 'DECIDE' && metadata.answers ? Object.entries(metadata.answers).map(([key, answer]) => `<div><strong>${escapeHtml(key)}</strong> ${escapeHtml(answer.value === true ? 'YES' : answer.value === false ? 'NO' : answer.value)} · ${Math.round((answer.confidence || 0) * 100)}%</div>`).join('') : '';
    return `<article class="trace-event"><div class="trace-index">${String(event.sequence).padStart(2, '0')}</div><div class="trace-main"><div class="trace-head"><strong>${escapeHtml(event.type)}</strong><span>${escapeHtml(String(event.duration_ms ?? 0))}ms · ${escapeHtml(event.status)}</span></div><div class="trace-label">${escapeHtml(label)}</div>${details}</div></article>`;
  }).join('');
  return `<section class="lab-section runtime-trace"><div class="lab-section-head"><div><span class="eyebrow">RUNTIME TRACE</span><h3>Observable execution</h3></div><span class="lab-count">${state.runs.length} runs</span></div>${runs}${events}${runs || events ? '' : '<div class="lab-empty">No hay trazas todavía.</div>'}</section>`;
}

function renderModels() {
  return `<section class="lab-section"><div class="lab-section-head"><div><span class="eyebrow">PROVIDERS / MODELS</span><h3>Model inventory</h3></div><span class="lab-count">${allModels().length} models</span></div>${state.providers.length ? state.providers.map((provider) => `<article class="model-provider"><div class="model-provider-head"><div><h4>${escapeHtml(provider.name)}</h4><span>${escapeHtml(provider.base_url)}</span></div><span class="status status-ready">● CONFIGURED</span></div><div class="model-list">${provider.models.map((model) => `<div class="model-row"><strong>${escapeHtml(model.id)}</strong><span>${escapeHtml(provider.name)}</span>${(model.capabilities || []).map((capability) => `<em>${escapeHtml(capability)}</em>`).join('')}</div>`).join('') || '<div class="lab-empty">No hay modelos descubiertos.</div>'}</div></article>`).join('') : '<div class="lab-empty">No hay providers configurados. Añade uno desde Settings para inspeccionar sus modelos.</div>'}</section>`;
}

function renderTools() {
  const native = state.tools.filter((tool) => tool.source !== 'mcp');
  const mcp = state.tools.filter((tool) => tool.source === 'mcp');
  const group = (title, items) => items.length ? `<section class="tool-group"><div class="lab-section-head"><h3>${title}</h3><span class="lab-count">${items.length}</span></div>${items.map((tool) => `<details class="tool-row"><summary><strong>${escapeHtml(tool.name)}</strong><span>${escapeHtml(tool.module_id || 'native')}</span></summary><p>${escapeHtml(tool.description)}</p><pre>${escapeHtml(JSON.stringify(tool.parameters, null, 2))}</pre></details>`).join('')}</section>` : '';
  return `<section class="lab-section"><div class="lab-section-head"><div><span class="eyebrow">TOOL REGISTRY</span><h3>Read-only catalog</h3></div><span class="lab-count">${state.tools.length} tools</span></div>${group('Native', native)}${group('MCP', mcp)}${!state.tools.length ? '<div class="lab-empty">No hay tools activas para inspeccionar.</div>' : ''}</section>`;
}

function renderDecisions() {
  const module = state.modules.find((item) => item.id === 'decision-runtime');
  return `<section class="lab-section"><div class="lab-section-head"><div><span class="eyebrow">DECISION RUNTIME</span><h3>Decision playground</h3></div><span class="status ${module?.status?.available ? 'status-ready' : 'status-disabled'}">● ${module?.status?.available ? 'READY' : 'UNAVAILABLE'}</span></div><form id="decision-form" class="decision-form"><label>State<textarea id="decision-state" required placeholder="Describe the situation to evaluate..."></textarea></label><div id="decision-questions"></div><button type="button" class="text-button" id="add-question">+ Add question</button><div class="form-actions"><button class="primary-button" type="submit" ${module?.status?.available ? '' : 'disabled'}>Run decision</button></div></form><pre class="decision-result" id="decision-result" hidden></pre></section>`;
}

function bindDecisionPlayground() {
  const questions = [{ id: 'question', type: 'boolean', statement: '' }];
  const renderQuestions = () => { $('#decision-questions').innerHTML = questions.map((question, index) => `<div class="decision-question"><div class="decision-question-line"><input data-question="id" data-index="${index}" value="${escapeHtml(question.id)}" placeholder="id"><select data-question="type" data-index="${index}"><option value="boolean" ${question.type === 'boolean' ? 'selected' : ''}>boolean</option><option value="choice" ${question.type === 'choice' ? 'selected' : ''}>choice</option><option value="score" ${question.type === 'score' ? 'selected' : ''}>score</option></select><input data-question="statement" data-index="${index}" value="${escapeHtml(question.statement)}" placeholder="Question statement" required></div>${question.type === 'choice' ? `<input data-question="options" data-index="${index}" value="${escapeHtml(question.options || '')}" placeholder="Options, comma separated">` : ''}${question.type === 'score' ? `<input data-question="scale" data-index="${index}" value="${escapeHtml(question.scale || '')}" placeholder="Scale, comma separated">` : ''}</div>`).join(''); };
  renderQuestions();
  $('#add-question').onclick = () => { questions.push({ id: `question_${questions.length + 1}`, type: 'boolean', statement: '' }); renderQuestions(); };
  $('#decision-questions').oninput = (event) => { const input = event.target; const question = questions[Number(input.dataset.index)]; if (question) question[input.dataset.question] = input.value; };
  $('#decision-questions').onchange = (event) => { const input = event.target; const question = questions[Number(input.dataset.index)]; if (question) { question[input.dataset.question] = input.value; renderQuestions(); } };
  $('#decision-form').onsubmit = async (event) => { event.preventDefault(); const result = $('#decision-result'); try { result.hidden = false; result.textContent = 'Running...'; const payload = questions.map((question) => { const item = { id: question.id, type: question.type, statement: question.statement }; if (question.type === 'choice') item.options = question.options.split(',').map((value) => value.trim()).filter(Boolean); if (question.type === 'score') item.scale = question.scale.split(',').map((value) => value.trim()).filter(Boolean); return item; }); const data = await api('/decisions', { method: 'POST', body: JSON.stringify({ state: $('#decision-state').value, questions: payload }) }); result.textContent = JSON.stringify(data, null, 2); } catch (error) { result.hidden = false; result.textContent = error.message; } };
}

function resetForm() { $('#provider-id').value = ''; $('#provider-name').value = ''; $('#provider-url').value = ''; $('#provider-key').value = ''; $('#provider-models').value = ''; $('#form-title').textContent = 'Añadir proveedor'; $('#save-provider').textContent = 'Guardar proveedor'; $('#cancel-edit').hidden = true; }
function renderProviderList() {
  const list = $('#provider-list');
  if (!state.providers.length) { list.innerHTML = '<div class="empty-providers">Todavía no hay proveedores. Añade oMLX, OpenAI, DeepSeek o cualquier API compatible.</div>'; return; }
    list.innerHTML = state.providers.map((provider) => `<article class="provider-card"><div class="provider-card-head"><span class="provider-bullet"></span><strong>${escapeHtml(provider.name)}</strong><span class="provider-url">${escapeHtml(provider.base_url)}</span><div class="card-actions"><button data-action="refresh" data-id="${provider.id}">↻ Detectar</button><button data-action="edit" data-id="${provider.id}">Editar</button><button data-action="delete" data-id="${provider.id}" aria-label="Eliminar ${escapeHtml(provider.name)}">×</button></div></div><div class="model-list settings-model-list">${provider.models.map((model) => `<details class="settings-model"><summary><strong>${escapeHtml(model.id)}</strong><span>${(model.capabilities || []).map((capability) => escapeHtml(capability)).join(', ') || 'Generation'}</span><button type="button" class="model-menu" aria-label="Editar capacidades de ${escapeHtml(model.id)}">⋮</button></summary><div class="model-capabilities"><span>Capabilities</span><label><input type="checkbox" data-action="model-capability" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" data-capability="embedding" ${(model.capabilities || []).includes('embedding') ? 'checked' : ''}> Embeddings</label><label><input type="checkbox" data-action="model-capability" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" data-capability="tool-calling" ${(model.capabilities || []).includes('tool-calling') ? 'checked' : ''}> Tool calling</label><label><input type="checkbox" data-action="model-capability" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" data-capability="thinking" ${(model.capabilities || []).includes('thinking') ? 'checked' : ''}> Thinking</label><label><input type="checkbox" data-action="model-capability" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" data-capability="thinking-budget" ${(model.capabilities || []).includes('thinking-budget') ? 'checked' : ''}> Thinking budget</label><label><input type="checkbox" data-action="model-capability" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" data-capability="reasoning-content" ${(model.capabilities || []).includes('reasoning-content') ? 'checked' : ''}> Reasoning display</label><button type="button" class="text-button model-delete" data-action="model-delete" data-id="${provider.id}" data-model="${escapeHtml(model.id)}">Remove model</button></div></details>`).join('') || '<span class="optional">Sin modelos todavía</span>'}</div></article>`).join('');
   list.querySelectorAll('.settings-model').forEach((detail) => { const model = detail.querySelector('summary strong')?.textContent || ''; const providerId = detail.closest('.provider-card')?.querySelector('[data-action="refresh"]')?.dataset.id; const owner = state.providers.find((item) => item.id === providerId); const capabilities = owner?.models.find((item) => item.id === model)?.capabilities || []; const row = detail.querySelector('.model-capabilities'); if (row && !row.querySelector('[data-capability="reranking"]')) row.insertAdjacentHTML('beforeend', `<label><input type="checkbox" data-action="model-capability" data-id="${providerId}" data-model="${escapeHtml(model)}" data-capability="reranking" ${capabilities.includes('reranking') ? 'checked' : ''}> Reranking</label>`); });
   list.querySelectorAll('[data-action]').forEach((button) => { button.onclick = () => providerAction(button); });
}

async function providerAction(button) {
  const provider = state.providers.find((item) => item.id === button.dataset.id);
  try {
    if (button.dataset.action === 'refresh') { button.textContent = '…'; await api(`/providers/${provider.id}/refresh`, { method: 'POST' }); await loadProviders(); await loadKnowledge(); toast('Modelos actualizados'); }
    if (button.dataset.action === 'delete' && confirm(`¿Eliminar ${provider.name} y sus modelos?`)) { await api(`/providers/${provider.id}`, { method: 'DELETE' }); await loadProviders(); }
    if (button.dataset.action === 'model-delete') { await api(`/providers/${provider.id}/models/${encodeURIComponent(button.dataset.model)}`, { method: 'DELETE' }); await loadProviders(); }
    if (button.dataset.action === 'model-capability') { const model = provider.models.find((item) => item.id === button.dataset.model); const capabilities = new Set(model?.capabilities || []); button.checked ? capabilities.add(button.dataset.capability) : capabilities.delete(button.dataset.capability); await api(`/providers/${provider.id}/models/${encodeURIComponent(button.dataset.model)}`, { method: 'PATCH', body: JSON.stringify({ capabilities: [...capabilities] }) }); await loadProviders(); await loadKnowledge(); toast(`${button.dataset.capability} capability ${button.checked ? 'enabled' : 'disabled'}`); }
    if (button.dataset.action === 'edit') { $('#provider-id').value = provider.id; $('#provider-name').value = provider.name; $('#provider-url').value = provider.base_url; $('#provider-key').value = ''; $('#provider-models').value = provider.models.map((model) => model.id).join('\n'); $('#form-title').textContent = 'Editar proveedor'; $('#save-provider').textContent = 'Guardar cambios'; $('#cancel-edit').hidden = false; $('#provider-name').focus(); }
  } catch (error) { toast(error.message); }
}

$('#provider-form').onsubmit = async (event) => {
  event.preventDefault();
  const id = $('#provider-id').value;
  const body = { name: $('#provider-name').value, base_url: $('#provider-url').value, api_key: $('#provider-key').value, models: $('#provider-models').value.split('\n').map((model) => model.trim()).filter(Boolean) };
  try { await api(`/providers${id ? `/${id}` : ''}`, { method: id ? 'PUT' : 'POST', body: JSON.stringify(body) }); resetForm(); await loadProviders(); toast(id ? 'Proveedor actualizado' : 'Proveedor añadido'); } catch (error) { toast(error.message); }
};

$('#embedding-form').onsubmit = async (event) => {
  event.preventDefault();
  const finalTopK = Number($('#embedding-final-top-k').value);
  const rerankerConfigured = rerankerState((state.knowledge?.providers || state.providers), $('#reranker-provider').value, $('#reranker-model').value, $('#reranking-enabled').checked).configured;
  const body = { provider_id: $('#embedding-provider').value, model_id: $('#embedding-model').value, target_chunk_size: Number($('#embedding-target').value), max_chunk_size: Number($('#embedding-max').value), overlap: Number($('#embedding-overlap').value), batch_size: Number($('#embedding-batch').value), retrieval_mode: $('#embedding-mode').value, dense_candidate_limit: Number($('#embedding-dense-candidates').value), lexical_candidate_limit: Number($('#embedding-lexical-candidates').value), rrf_k: Number($('#embedding-rrf-k').value), final_top_k: finalTopK, retrieval_top_k: finalTopK, retrieval_max_context_chars: Number($('#embedding-context').value), reranking_enabled: $('#reranking-enabled').checked && rerankerConfigured, reranker_provider_id: $('#reranker-provider').value, reranker_model: $('#reranker-model').value, reranker_candidate_limit: Number($('#reranker-candidates').value), reranker_timeout_ms: Number($('#reranker-timeout').value) };
  try { const result = await api('/settings/embeddings', { method: 'PUT', body: JSON.stringify(body) }); await loadKnowledge(); toast(result.invalidated ? 'Saved; indexes are outdated' : 'Knowledge configuration saved'); } catch (error) { toast(error.message); }
};
$('#embedding-mode').onchange = updateRetrievalControls;
$('#test-embedding').onclick = async () => { try { const result = await api('/settings/embeddings/test', { method: 'POST' }); toast(`${result.provider} / ${result.model} · ${result.status} · ${result.dimension}d · ${result.latency_ms}ms`); } catch (error) { toast(error.message); } };

$('#agent-form').onsubmit = async (event) => {
  event.preventDefault();
  const id = $('#agent-id').value;
  const [provider_id, model_id] = $('#agent-model').value.split('::');
  const temperature = $('#agent-temperature').value;
  const body = { name: $('#agent-name').value, description: $('#agent-description').value, provider_id, model_id, system_instructions: $('#agent-instructions').value, model_parameters: temperature === '' ? {} : { temperature: Number(temperature) }, tool_names: [...$('#agent-tool-options').querySelectorAll('input:checked')].map((input) => input.value) };
  try { await api(`/agents${id ? `/${id}` : ''}`, { method: id ? 'PATCH' : 'POST', body: JSON.stringify(body) }); resetAgentForm(); await loadAgents(); toast(id ? 'Agent updated' : 'Agent created'); } catch (error) { toast(error.message); }
};

$('#notebook-form').onsubmit = async (event) => {
  event.preventDefault();
  const id = $('#notebook-id').value;
  try { await api(`/notebooks${id ? `/${id}` : ''}`, { method: id ? 'PATCH' : 'POST', body: JSON.stringify({ name: $('#notebook-name').value, description: $('#notebook-description').value }) }); resetNotebookForm(); await loadNotebooks(); toast(id ? 'Notebook updated' : 'Notebook created'); } catch (error) { toast(error.message); }
};

$('#model-select').onchange = () => { updateComposerModel(); if ($('#model-select').value) savePreference('last_chat_model', $('#model-select').value); };
document.querySelectorAll('[data-mode]').forEach((button) => { button.onclick = () => setExecutionMode(button.dataset.mode); });
['web-chip', 'tools-chip'].forEach((id) => { $(`#${id}`).onclick = async () => { const key = id === 'web-chip' ? 'webEnabled' : 'toolsEnabled'; state[key] = !state[key]; $(`#${id}`).setAttribute('aria-pressed', String(state[key])); }; });
$('#notebook-picker').onchange = async (event) => {
  state.currentNotebookId = event.target.value || null;
  if (!state.conversationId) return;
  try { await api(`/conversations/${state.conversationId}`, { method: 'PATCH', body: JSON.stringify({ notebook_id: state.currentNotebookId }) }); await loadChats(); } catch (error) { toast(error.message); }
};
$('#config-inspector').onclick = () => { $('#config-content').innerHTML = renderConfig(); openSurface('config'); bindConfig(); };
$('#cancel-edit').onclick = resetForm;
$('#open-settings').onclick = () => { closeSidebar(); openSurface('settings'); };
$('#agent-picker').onclick = () => { openSurface('agents'); $('#agent-picker').setAttribute('aria-expanded', 'true'); };
$('#open-agents').onclick = () => { closeSidebar(); openSurface('agents'); };
$('#open-knowledge').onclick = () => { closeSidebar(); openSurface('notebooks'); };
$('#open-lab').onclick = () => { closeSidebar(); openSurface('lab'); };
$('#open-notebooks').onclick = () => { closeSidebar(); openSurface('notebooks'); };
$('#new-chat').onclick = beginChat;
$('#new-agent').onclick = () => editAgent();
$('#cancel-agent-edit').onclick = resetAgentForm;
$('#new-notebook').onclick = () => editNotebook();
$('#cancel-notebook-edit').onclick = resetNotebookForm;
$('#new-chat-top').onclick = beginChat;
$('#send-button').onclick = send;
$('#prompt').onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } };
$('#prompt').oninput = (event) => { event.target.style.height = 'auto'; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; };
$('#attach-button').onclick = () => $('#file-input').click();
$('#file-input').onchange = async (event) => { for (const file of event.target.files) { try { const form = new FormData(); form.append('file', file); state.attachments.push(await api('/files', { method: 'POST', body: form })); } catch (error) { toast(error.message); } } renderAttachments(); event.target.value = ''; };
document.querySelectorAll('.suggestion').forEach((button) => { button.onclick = () => { $('#prompt').value = button.dataset.prompt; $('#prompt').dispatchEvent(new Event('input')); $('#prompt').focus(); }; });
$('#refresh-chats').onclick = loadChats;
$('#chat-search').oninput = renderChats;
$('#chat-view').addEventListener('scroll', () => { state.followingBottom = isNearBottom(); updateScrollButton(); }, { passive: true });
$('#scroll-bottom').onclick = () => scrollToBottom();
$('#open-sidebar').onclick = openSidebar;
$('#close-sidebar').onclick = closeSidebar;
$('#sidebar-backdrop').onclick = closeSidebar;
$('#surface-backdrop').onclick = (event) => { if (event.target === $('#surface-backdrop')) closeSurface(); };
document.querySelectorAll('[data-close-surface]').forEach((button) => { button.onclick = closeSurface; });
document.querySelectorAll('input[name="ui-style"]').forEach((input) => { input.onchange = () => { document.documentElement.dataset.ui = input.value; applyPreferences(true); }; });
document.querySelectorAll('input[name="color-scheme"]').forEach((input) => { input.onchange = () => { document.documentElement.dataset.colorScheme = input.value; applyPreferences(true); }; });
document.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); beginChat(); } if (event.key === 'Escape') { closeSurface(); closeSidebar(); } });

applyPreferences();
(async () => {
  await loadPreferences();
  const results = await Promise.allSettled([loadModules(), loadProviders(), loadKnowledge(), loadTools(), loadAgents(), loadNotebooks(), loadShadow(), loadTraces(), loadChats()]);
  const failure = results.find((result) => result.status === 'rejected');
  if (failure) toast(failure.reason?.message || 'No se pudieron cargar todos los datos.');
})();
