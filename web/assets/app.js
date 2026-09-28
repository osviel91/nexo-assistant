import { renderMarkdown } from './markdown.js';
import { effectiveMessageIdentity } from './identity.js';

const $ = (selector) => document.querySelector(selector);
const state = { providers: [], agents: [], conversations: [], notebooks: [], notebookSources: [], currentNotebookId: null, modules: [], tools: [], shadow: [], traces: [], runs: [], labTab: 'models', conversationId: null, agentProfileId: null, messages: [], attachments: [], busy: false, lastRuntime: null, activity: null };
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

function moduleEnabled(id) { return state.modules.some((module) => module.id === id); }
function allModels() { return state.providers.flatMap((provider) => provider.models.map((model) => ({ provider, model }))); }
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
  if (allModels().some(({ provider, model }) => `${provider.id}::${model.id}` === oldValue)) select.value = oldValue;
  updateComposerModel();
}

function renderNotebookPicker() {
  const select = $('#notebook-picker');
  if (!select) return;
  select.innerHTML = `<option value="">No notebook</option>${state.notebooks.map((notebook) => `<option value="${escapeHtml(notebook.id)}">${escapeHtml(notebook.name)} · ${notebook.source_count} sources</option>`).join('')}`;
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

async function loadTools() {
  state.tools = await api('/tools');
  renderLab();
}

async function loadAgents() { state.agents = await api('/agents'); renderAgentPicker(); renderAgentList(); }

async function loadNotebooks() { state.notebooks = await api('/notebooks'); renderNotebookPicker(); renderNotebooks(); }
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
  try { const [notebook, sources] = await Promise.all([api(`/notebooks/${id}`), api(`/notebooks/${id}/sources`)]); state.currentNotebookId = id; state.notebookSources = sources; $('#notebook-detail').hidden = false; renderNotebookDetail(notebook); } catch (error) { toast(error.message); }
}
function renderNotebookDetail(notebook) {
  const status = { added: ['○ Added', 'status-disabled'], pending: ['○ Pending', 'status-disabled'], extracting: ['◌ Processing', 'status-connected'], ready: ['✓ Ready', 'status-ready'], failed: ['⚠ Extraction failed', 'status-error'] };
  $('#notebook-detail').innerHTML = `<button class="text-button notebook-back" id="notebook-back">← Notebooks</button><div class="notebook-detail-head"><div><span class="eyebrow">NOTEBOOK</span><h3>${escapeHtml(notebook.name)}</h3><p>${escapeHtml(notebook.description || 'Canonical documents are ready for future knowledge processing.')}</p></div><span class="status status-ready">INGESTION AVAILABLE</span></div><div class="notebook-sources-head"><h4>Sources</h4><span>${state.notebookSources.length}</span></div><div class="notebook-source-list">${state.notebookSources.map((source) => { const [label, className] = status[source.status] || [source.status, 'status-disabled']; const action = ['added', 'failed'].includes(source.status) ? `<button class="text-button" data-source-ingest="${escapeHtml(source.id)}">${source.status === 'failed' ? 'Retry' : 'Process'}</button>` : ''; const indexAction = source.status === 'ready' && source.indexing_status !== 'indexing' ? `<button class="text-button" data-source-index="${escapeHtml(source.id)}">${source.indexing_status === 'failed' ? 'Retry index' : 'Index'}</button>` : ''; const indexLabel = source.indexing_status === 'ready' ? `✓ Indexed · ${source.chunk_count || 0} chunks` : source.indexing_status === 'failed' ? '⚠ Index failed' : ''; return `<article class="notebook-source"><div><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(source.type)} · <span class="status ${className}">${label}</span>${source.metadata?.mime ? ` · ${escapeHtml(source.metadata.mime)}` : ''}${indexLabel ? ` · ${escapeHtml(indexLabel)}` : ''}</small>${source.status === 'failed' && source.error_message ? `<small>${escapeHtml(source.error_message)}</small>` : ''}</div><div>${action}${indexAction}<button data-source-delete="${escapeHtml(source.id)}" aria-label="Remove ${escapeHtml(source.title)}">×</button></div></article>`; }).join('') || '<div class="empty-providers">Añade una fuente para convertirla en un documento canónico.</div>'}</div><div class="source-add"><h4>Add source</h4><form id="notebook-file-form"><label>File<input id="notebook-file" type="file" required accept=".pdf,.md,.txt"></label><button class="primary-button" type="submit">Add file</button></form><form id="notebook-web-form"><label>Web URL<input id="notebook-url" type="url" required placeholder="https://example.com/section"></label><label>Title<input id="notebook-web-title" required placeholder="Source title"></label><button class="text-button" type="submit">Add web source</button></form></div>`;
  $('#notebook-back').onclick = () => { $('#notebook-detail').hidden = true; state.currentNotebookId = null; };
  const ingestSource = async (sourceId) => { try { await api(`/notebooks/${state.currentNotebookId}/sources/${sourceId}/ingest`, { method: 'POST' }); await openNotebook(state.currentNotebookId); await loadNotebooks(); } catch (error) { toast(error.message); } };
  $('#notebook-file-form').onsubmit = async (event) => { event.preventDefault(); const file = $('#notebook-file').files[0]; if (!file) return; const form = new FormData(); form.append('file', file); try { const source = await api(`/notebooks/${state.currentNotebookId}/sources`, { method: 'POST', body: form }); await ingestSource(source.id); toast('File processed'); } catch (error) { toast(error.message); } };
  $('#notebook-web-form').onsubmit = async (event) => { event.preventDefault(); try { const source = await api(`/notebooks/${state.currentNotebookId}/sources`, { method: 'POST', body: JSON.stringify({ type: 'web', title: $('#notebook-web-title').value, url: $('#notebook-url').value }) }); await ingestSource(source.id); } catch (error) { toast(error.message); } };
  $('#notebook-detail').querySelectorAll('[data-source-ingest]').forEach((button) => { button.onclick = () => ingestSource(button.dataset.sourceIngest); });
  $('#notebook-detail').querySelectorAll('[data-source-index]').forEach((button) => { button.onclick = async () => { try { await api(`/notebooks/${state.currentNotebookId}/sources/${button.dataset.sourceIndex}/index`, { method: 'POST' }); await openNotebook(state.currentNotebookId); } catch (error) { toast(error.message); } }; });
  $('#notebook-detail').querySelectorAll('[data-source-delete]').forEach((button) => { button.onclick = async () => { try { await api(`/notebooks/${state.currentNotebookId}/sources/${button.dataset.sourceDelete}`, { method: 'DELETE' }); await openNotebook(state.currentNotebookId); await loadNotebooks(); } catch (error) { toast(error.message); } }; });
}

function currentAgent() { return state.agents.find((agent) => agent.id === state.agentProfileId); }
function renderAgentPicker() {
  const agent = currentAgent();
  $('#agent-picker-name').textContent = agent?.name || 'Nexo';
}
function renderAgentList() {
  const items = [{ id: '', name: 'Nexo', description: 'General assistant' }, ...state.agents];
  $('#agent-list').innerHTML = `${items.map((agent) => {
    const stored = Boolean(agent.id);
    const warning = stored && !agent.model_available ? '<span class="status status-degraded">Model unavailable</span>' : stored && agent.unavailable_tools?.length ? `<span class="status status-degraded">${agent.unavailable_tools.length} tool${agent.unavailable_tools.length > 1 ? 's' : ''} unavailable</span>` : '';
    return `<div class="agent-option ${(!state.agentProfileId && !stored) || agent.id === state.agentProfileId ? 'selected' : ''}" data-agent-id="${escapeHtml(agent.id)}" role="button" tabindex="0"><span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.description || '')}</small>${warning}</span>${stored ? `<span class="agent-actions"><span class="agent-tools-count">${agent.tool_names.length} tools</span><button type="button" data-edit-agent="${escapeHtml(agent.id)}">Edit</button><button type="button" data-delete-agent="${escapeHtml(agent.id)}">Delete</button></span>` : '<span class="agent-check">✓</span>'}</div>`;
  }).join('')}<p class="agent-help">Select Nexo to clear this conversation's binding.</p>`;
  $('#agent-list').querySelectorAll('[data-agent-id]').forEach((button) => { button.onclick = () => selectAgent(button.dataset.agentId || null); });
  $('#agent-list').querySelectorAll('[data-edit-agent]').forEach((button) => { button.onclick = (event) => { event.stopPropagation(); editAgent(button.dataset.editAgent); }; });
  $('#agent-list').querySelectorAll('[data-delete-agent]').forEach((button) => { button.onclick = async (event) => { event.stopPropagation(); await deleteAgent(button.dataset.deleteAgent); }; });
}
async function selectAgent(id) {
  state.agentProfileId = id || null;
  renderAgentPicker(); renderAgentList();
  if (state.conversationId) try { await api(`/conversations/${state.conversationId}`, { method: 'PATCH', body: JSON.stringify({ agent_profile_id: state.agentProfileId }) }); await loadChats(); } catch (error) { toast(error.message); }
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
  $('#conversation-list').innerHTML = chats.map((chat) => `<button class="conversation-item ${chat.id === state.conversationId ? 'selected' : ''}" data-id="${escapeHtml(chat.id)}" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title)}</button>`).join('') || '<div class="empty-providers">No hay chats todavía</div>';
  document.querySelectorAll('.conversation-item').forEach((button) => { button.onclick = () => openChat(button.dataset.id); });
}

function beginChat() {
  state.conversationId = null;
  state.messages = [];
  state.attachments = [];
  state.agentProfileId = null;
  state.currentNotebookId = null;
  state.lastRuntime = null;
  $('#messages').innerHTML = '';
  $('#welcome').hidden = false;
  renderAttachments();
  renderChats();
  closeSidebar();
  $('#prompt').focus();
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
    const notebookList = citations.length ? `<details class="message-sources notebook-sources"><summary>Notebook sources · ${citations.length}</summary>${citations.map((citation) => `<div><strong>[${escapeHtml(citation.citation_key)}]</strong> ${escapeHtml(citation.source_title || 'Notebook source')}<span>${escapeHtml(citation.status || citation.provenance?.[0]?.heading || 'Retrieved excerpt')}</span></div>`).join('')}</details>` : '';
    const content = message.role === 'assistant' ? renderMarkdown(message.content) : escapeHtml(message.content).replace(/\n/g, '<br>');
    const runtime = message.runtime || {};
    const metrics = message.role === 'assistant' && (runtime.tokens_per_second != null || runtime.completion_tokens != null || runtime.context_used_tokens != null)
      ? `<div class="message-metrics">${runtime.tokens_per_second != null ? `${escapeHtml(runtime.tokens_per_second)} tok/s` : ''}${runtime.completion_tokens != null ? ` · ${escapeHtml(runtime.completion_tokens)} tok` : ''}${runtime.context_used_tokens != null && runtime.context_window != null ? ` · ctx ${escapeHtml(runtime.context_used_tokens)}/${escapeHtml(runtime.context_window)}` : ''}</div>` : '';
     const activity = message === state.messages.at(-1) && message.role === 'assistant' && state.activity ? `<div class="message-activity"><span class="activity-dot"></span>${escapeHtml(activityLabel(state.activity))}</div>` : '';
     return `<article class="message ${message.role === 'user' ? 'user' : ''}">${message.role === 'assistant' ? '<div class="avatar-small" aria-hidden="true">n</div>' : ''}<div class="message-body">${message.role === 'assistant' ? `<div class="message-meta">${escapeHtml(effectiveMessageIdentity(message))}</div>` : ''}<div class="message-content">${content}</div>${activity}${metrics}${attachments}${sourceList}${notebookList}</div></article>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

function activityLabel(activity) {
  if (activity.type === 'RETRIEVE') return 'Consultando el notebook';
  if (activity.type === 'ACT') return `Usando herramienta ${activity.tool || ''}`.trim();
  return 'Procesando respuesta';
}

function updateStreamingAnswer(answer) {
  const content = $('#messages').querySelector('.message:last-child .message-content');
  if (content) content.innerHTML = renderMarkdown(answer);
  $('#messages').scrollTop = $('#messages').scrollHeight;
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
    state.agentProfileId = data.conversation.agent_profile_id || null;
    state.currentNotebookId = data.conversation.notebook_id || null;
    renderNotebookPicker();
    state.messages = data.messages;
    state.lastRuntime = [...state.messages].reverse().find((message) => message.role === 'assistant')?.runtime || null;
    $('#welcome').hidden = true;
    renderMessages();
    renderChats();
    closeSidebar();
  } catch (error) { toast(error.message); }
}

function renderAttachments() {
  $('#attachment-tray').innerHTML = state.attachments.map((attachment, index) => `<span class="attachment-chip">${escapeHtml(attachment.name)}<button data-index="${index}" aria-label="Quitar ${escapeHtml(attachment.name)}">×</button></span>`).join('');
  document.querySelectorAll('.attachment-chip button').forEach((button) => { button.onclick = () => { state.attachments.splice(Number(button.dataset.index), 1); renderAttachments(); }; });
}

async function send() {
  if (state.busy) return;
  const text = $('#prompt').value.trim();
  const choice = selected();
  const agent = currentAgent();
  const effectiveChoice = agent ? { provider: state.providers.find((provider) => provider.id === agent.provider_id), model: agent.model_id } : choice;
  if (!text && !state.attachments.length) return;
   if (!effectiveChoice.provider && !state.agentProfileId) { openSurface('settings'); toast('Configura un proveedor y selecciona un modelo'); return; }
  state.busy = true;
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
     const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation_id: state.conversationId, provider_id: effectiveChoice.provider?.id || '', model_id: effectiveChoice.model || '', content: text, attachments, agent_profile_id: state.agentProfileId, notebook_id: state.currentNotebookId }) });
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
         if (data.activity) updateStreamingActivity(data.activity);
         if (data.delta) { answer += data.delta; state.messages.at(-1).content = answer; updateStreamingAnswer(answer); }
         if (data.done) { state.conversationId = data.conversation_id; state.lastRuntime = data.runtime || null; state.activity = null; Object.assign(state.messages.at(-1), { content: answer, provider_id: data.provider_id, model_id: data.model_id, sources: data.sources || [], citations: data.citations || [], runtime: data.runtime || {} }); renderMessages(); }
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
  if (name === 'settings') { resetForm(); applyPreferences(); renderProviderList(); $('#provider-name').focus(); }
  if (name === 'agents') { resetAgentForm(); renderAgentList(); }
  if (name === 'notebooks') { resetNotebookForm(); renderNotebooks(); }
}

function renderConfig() {
  const runtime = state.lastRuntime;
  if (!runtime) return '<div class="lab-empty">No completed run yet.</div>';
  const row = (label, value) => value == null || value === '' ? '' : `<div class="config-value"><span>${label}</span> ${escapeHtml(value)}</div>`;
  return `<div class="config-block">${row('Agent', runtime.agent_profile_name || 'Nexo')}${row('Provider', runtime.resolved_provider)}${row('Model', runtime.resolved_model)}${row('Instructions', runtime.system_instructions_applied ? 'Applied' : 'Not applied')}</div><div class="config-block"><h3>TELEMETRY</h3>${row('Generation', runtime.completion_tokens != null ? `${runtime.completion_tokens} tokens` : null)}${row('Tokens/s', runtime.tokens_per_second)}${row('TTFT ms', runtime.ttft_ms)}${row('Context', runtime.context_used_tokens != null && runtime.context_window != null ? `${runtime.context_used_tokens} / ${runtime.context_window}` : null)}${row('Notebook chunks', runtime.retrieval_count)}${row('Top score', runtime.top_score)}${row('Context chars', runtime.context_chars)}${row('Truncated', runtime.context_truncated ? 'Yes' : runtime.retrieval_count != null ? 'No' : null)}</div><div class="config-block"><h3>PARAMETERS</h3>${row('temperature', runtime.temperature)}${row('top_p', runtime.top_p)}${row('top_k', runtime.top_k)}</div><div class="config-block"><h3>TOOLS</h3>${row('effective', (runtime.effective_tool_names || []).join(', ') || 'none')}</div>`;
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
  list.innerHTML = state.providers.map((provider) => `<article class="provider-card"><div class="provider-card-head"><span class="provider-bullet"></span><strong>${escapeHtml(provider.name)}</strong><span class="provider-url">${escapeHtml(provider.base_url)}</span><div class="card-actions"><button data-action="refresh" data-id="${provider.id}">↻ Detectar</button><button data-action="edit" data-id="${provider.id}">Editar</button><button data-action="delete" data-id="${provider.id}" aria-label="Eliminar ${escapeHtml(provider.name)}">×</button></div></div><div class="model-pills">${provider.models.map((model) => `<span class="model-pill">${escapeHtml(model.id)}<button data-action="model-delete" data-id="${provider.id}" data-model="${escapeHtml(model.id)}" aria-label="Quitar ${escapeHtml(model.id)}">×</button></span>`).join('') || '<span class="optional">Sin modelos todavía</span>'}</div></article>`).join('');
  list.querySelectorAll('[data-action]').forEach((button) => { button.onclick = () => providerAction(button); });
}

async function providerAction(button) {
  const provider = state.providers.find((item) => item.id === button.dataset.id);
  try {
    if (button.dataset.action === 'refresh') { button.textContent = '…'; await api(`/providers/${provider.id}/refresh`, { method: 'POST' }); await loadProviders(); toast('Modelos actualizados'); }
    if (button.dataset.action === 'delete' && confirm(`¿Eliminar ${provider.name} y sus modelos?`)) { await api(`/providers/${provider.id}`, { method: 'DELETE' }); await loadProviders(); }
    if (button.dataset.action === 'model-delete') { await api(`/providers/${provider.id}/models/${encodeURIComponent(button.dataset.model)}`, { method: 'DELETE' }); await loadProviders(); }
    if (button.dataset.action === 'edit') { $('#provider-id').value = provider.id; $('#provider-name').value = provider.name; $('#provider-url').value = provider.base_url; $('#provider-key').value = ''; $('#provider-models').value = provider.models.map((model) => model.id).join('\n'); $('#form-title').textContent = 'Editar proveedor'; $('#save-provider').textContent = 'Guardar cambios'; $('#cancel-edit').hidden = false; $('#provider-name').focus(); }
  } catch (error) { toast(error.message); }
}

$('#provider-form').onsubmit = async (event) => {
  event.preventDefault();
  const id = $('#provider-id').value;
  const body = { name: $('#provider-name').value, base_url: $('#provider-url').value, api_key: $('#provider-key').value, models: $('#provider-models').value.split('\n').map((model) => model.trim()).filter(Boolean) };
  try { await api(`/providers${id ? `/${id}` : ''}`, { method: id ? 'PUT' : 'POST', body: JSON.stringify(body) }); resetForm(); await loadProviders(); toast(id ? 'Proveedor actualizado' : 'Proveedor añadido'); } catch (error) { toast(error.message); }
};

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

$('#model-select').onchange = updateComposerModel;
$('#notebook-picker').onchange = async (event) => {
  state.currentNotebookId = event.target.value || null;
  if (!state.conversationId) return;
  try { await api(`/conversations/${state.conversationId}`, { method: 'PATCH', body: JSON.stringify({ notebook_id: state.currentNotebookId }) }); await loadChats(); } catch (error) { toast(error.message); }
};
$('#config-inspector').onclick = () => { $('#config-content').innerHTML = renderConfig(); openSurface('config'); };
$('#cancel-edit').onclick = resetForm;
$('#open-settings').onclick = () => { closeSidebar(); openSurface('settings'); };
$('#agent-picker').onclick = () => { openSurface('agents'); $('#agent-picker').setAttribute('aria-expanded', 'true'); };
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
$('#open-sidebar').onclick = openSidebar;
$('#close-sidebar').onclick = closeSidebar;
$('#sidebar-backdrop').onclick = closeSidebar;
$('#surface-backdrop').onclick = (event) => { if (event.target === $('#surface-backdrop')) closeSurface(); };
document.querySelectorAll('[data-close-surface]').forEach((button) => { button.onclick = closeSurface; });
document.querySelectorAll('input[name="ui-style"]').forEach((input) => { input.onchange = () => { document.documentElement.dataset.ui = input.value; applyPreferences(true); }; });
document.querySelectorAll('input[name="color-scheme"]').forEach((input) => { input.onchange = () => { document.documentElement.dataset.colorScheme = input.value; applyPreferences(true); }; });
document.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); beginChat(); } if (event.key === 'Escape') { closeSurface(); closeSidebar(); } });

applyPreferences();
Promise.all([loadModules(), loadProviders(), loadTools(), loadAgents(), loadNotebooks(), loadShadow(), loadTraces(), loadChats()]).catch((error) => toast(error.message));
