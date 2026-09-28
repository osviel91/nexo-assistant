const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

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
    .replace(/(?<!\*)(\*[^*\n]+\*)/g, (match) => `<em>${match.slice(1, -1)}</em>`)
    .replace(/(?<!_)(_([^_\n]+)_)(?!_)/g, (_, match) => `<em>${match.slice(1, -1)}</em>`);
  return text.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)]);
}

export function renderMarkdown(value) {
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
