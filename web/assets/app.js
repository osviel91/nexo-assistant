const $ = (selector) => document.querySelector(selector);
const state = { providers: [], conversations: [], modules: [], conversationId: null, messages: [], attachments: [], busy: false };
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

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function renderInlineMarkdown(value) {
  const tokens = [];
  const stash = (html) => { const token = `\u0000${tokens.length}\u0000`; tokens.push(html); return token; };
  let text = String(value ?? '').replace(/`([^`\n]+)`/g, (_, code) => stash(`<code>${escapeHtml(code)}</code>`));
  text = text.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+["']([^"']+)["'])?\)/g, (match, label, href, title) => {
    const url = safeUrl(href);
    return url ? stash(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(label)}</a>`) : match;
  });
  text = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/(?<!_)_([^_\n]+)_(?!_)/g, '<em>$1</em>');
  return text.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)]);
}

function renderMarkdown(value) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const fence = line.match(/^ {0,3}```\s*([\w+-]*)\s*$/);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^ {0,3}```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const language = fence[1] ? ` data-language="${escapeHtml(fence[1])}"` : '';
      output.push(`<pre${language}><code>${escapeHtml(code.join('\n'))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^ {0,3}(#{1,4})\s+(.+?)\s*#*$/);
    if (heading) { output.push(`<h${heading[1].length}>${renderInlineMarkdown(heading[2])}</h${heading[1].length}>`); index += 1; continue; }
    if (/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) { output.push('<hr>'); index += 1; continue; }
    if (/^ {0,3}>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^ {0,3}>/.test(lines[index])) quote.push(lines[index++].replace(/^ {0,3}>\s?/, ''));
      output.push(`<blockquote>${quote.map(renderInlineMarkdown).join('<br>')}</blockquote>`);
      continue;
    }
    const list = line.match(/^\s*([-*+])\s+(.+)$/) || line.match(/^\s*(\d+)[.)]\s+(.+)$/);
    if (list) {
      const ordered = /^\d/.test(list[1]);
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${renderInlineMarkdown(item[1])}</li>`); index += 1;
      }
      output.push(`<${ordered ? 'ol' : 'ul'}>${items.join('')}</${ordered ? 'ol' : 'ul'}>`);
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^ {0,3}(?:```|#{1,4}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[index]) && !/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(lines[index])) paragraph.push(lines[index++]);
    output.push(`<p>${paragraph.map(renderInlineMarkdown).join('<br>')}</p>`);
  }
  return output.join('');
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

function updateComposerModel() {
  // The active model is controlled from the header only.
}

async function loadProviders() {
  state.providers = await api('/providers');
  renderSelect();
  renderProviderList();
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
    const attachments = (message.attachments || []).map((attachment) => attachment.kind === 'image'
      ? `<img class="attachment-preview" src="${escapeHtml(attachment.data_url)}" alt="${escapeHtml(attachment.name)}">`
      : `<span class="message-model">Adjunto: ${escapeHtml(attachment.name)}</span>`).join('');
    const sourceList = sources.length ? `<details class="message-sources"><summary>Sources · ${sources.length}</summary>${sources.map((source) => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}<span>${escapeHtml(source.url)}</span></a>`).join('')}</details>` : '';
    const content = message.role === 'assistant' ? renderMarkdown(message.content) : escapeHtml(message.content).replace(/\n/g, '<br>');
    return `<article class="message ${message.role === 'user' ? 'user' : ''}">${message.role === 'assistant' ? '<div class="avatar-small" aria-hidden="true">n</div>' : ''}<div class="message-body">${message.role === 'assistant' ? `<div class="message-meta">NEXO · ${escapeHtml(message.model_id || '')}</div>` : ''}<div class="message-content">${content}</div>${attachments}${sourceList}</div></article>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

async function openChat(id) {
  try {
    const data = await api(`/conversations/${id}`);
    state.conversationId = id;
    state.messages = data.messages;
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
  if (!text && !state.attachments.length) return;
  if (!choice.provider) { openSurface('settings'); toast('Configura un proveedor y selecciona un modelo'); return; }
  state.busy = true;
  $('#send-button').disabled = true;
  $('#welcome').hidden = true;
  state.messages.push({ role: 'user', content: text, attachments: state.attachments.slice(), model_id: choice.model });
  renderMessages();
  $('#prompt').value = '';
  $('#prompt').style.height = '';
  const attachments = state.attachments.slice();
  state.attachments = [];
  renderAttachments();
  state.messages.push({ role: 'assistant', content: '', model_id: choice.model, sources: [] });
  renderMessages();
  let answer = '';
  try {
    const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation_id: state.conversationId, provider_id: choice.provider.id, model_id: choice.model, content: text, attachments }) });
    if (!response.ok) throw Error((await response.text()).slice(0, 300));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      for (const event of events) {
        const line = event.split('\n').find((item) => item.startsWith('data: '));
        if (!line) continue;
        const data = JSON.parse(line.slice(6));
        if (data.error) throw Error(data.error);
        if (data.status) toast(data.message);
        if (data.delta) { answer += data.delta; state.messages.at(-1).content = answer; renderMessages(); }
        if (data.done) { state.conversationId = data.conversation_id; Object.assign(state.messages.at(-1), { content: answer, provider_id: data.provider_id, model_id: data.model_id, sources: data.sources || [] }); renderMessages(); }
      }
    }
    if (!answer) state.messages.pop();
    await loadChats();
  } catch (error) {
    state.messages.at(-1).content = 'No se pudo completar la respuesta.';
    renderMessages();
    toast(error.message || 'No se pudo completar la respuesta.');
  } finally {
    state.busy = false;
    $('#send-button').disabled = false;
    $('#prompt').focus();
  }
}

function openSurface(name) {
  $('#surface-backdrop').hidden = false;
  $('#lab-surface').hidden = name !== 'lab';
  $('#settings-surface').hidden = name !== 'settings';
  if (name === 'settings') { resetForm(); applyPreferences(); renderProviderList(); $('#provider-name').focus(); }
}
function closeSurface() { $('#surface-backdrop').hidden = true; }
function openSidebar() { $('#sidebar').classList.add('open'); $('#sidebar-backdrop').hidden = false; $('#open-sidebar').setAttribute('aria-expanded', 'true'); }
function closeSidebar() { $('#sidebar').classList.remove('open'); $('#sidebar-backdrop').hidden = true; $('#open-sidebar').setAttribute('aria-expanded', 'false'); }

function renderLab() {
  const cards = [{ title: 'Models', text: 'Conecta y selecciona tus modelos locales o remotos.', status: `${allModels().length} configurados` }];
  if (moduleEnabled('decision-runtime')) {
    const module = state.modules.find((item) => item.id === 'decision-runtime');
    cards.push({ title: 'Decisions', text: 'Decisiones tipadas a través del runtime opcional.', status: module.status?.available ? 'Disponible · Arbiter' : 'No disponible', muted: !module.status?.available });
  }
  if (moduleEnabled('mcp')) cards.push({ title: 'Tools', text: 'Herramientas MCP conectadas a tu instalación.', status: 'Disponible' });
  if (moduleEnabled('web-search-searxng')) cards.push({ title: 'Web', text: 'Búsqueda web para modelos con tool-calling.', status: 'Disponible' });
  $('#lab-grid').innerHTML = cards.map((card) => `<article class="lab-card"><h3>${escapeHtml(card.title)}</h3><p>${escapeHtml(card.text)}</p><span class="lab-status ${card.muted ? 'muted' : ''}">${escapeHtml(card.status)}</span></article>`).join('');
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

$('#model-select').onchange = updateComposerModel;
$('#cancel-edit').onclick = resetForm;
$('#open-settings').onclick = () => { closeSidebar(); openSurface('settings'); };
$('#open-lab').onclick = () => { closeSidebar(); openSurface('lab'); };
$('#new-chat').onclick = beginChat;
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
Promise.all([loadModules(), loadProviders(), loadChats()]).catch((error) => toast(error.message));
