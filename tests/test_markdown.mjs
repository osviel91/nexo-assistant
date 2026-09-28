import assert from 'node:assert/strict';
import { renderMarkdown } from '../web/assets/markdown.js';

const raw = [
  '# Heading', '', 'Normal **bold** and *italic*.', '', '- item one', '- item two', '',
  '1. first', '2. second', '', '`inline code`', '', '```python', 'print("hello")', '```',
  '', '> blockquote', '', '[OpenAI](https://openai.com)',
].join('\n');

const rendered = renderMarkdown(raw);
assert.match(rendered, /<h1>Heading<\/h1>/);
assert.match(rendered, /<strong>bold<\/strong>/);
assert.match(rendered, /<em>italic<\/em>/);
assert.match(rendered, /<ul><li>item one<\/li><li>item two<\/li><\/ul>/);
assert.match(rendered, /<ol><li>first<\/li><li>second<\/li><\/ol>/);
assert.match(rendered, /<code>inline code<\/code>/);
assert.match(rendered, /<pre data-language="python"><code>print\(&quot;hello&quot;\)<\/code><\/pre>/);
assert.match(rendered, /<blockquote>blockquote<\/blockquote>/);
assert.match(rendered, /target="_blank" rel="noopener noreferrer"/);
assert.doesNotMatch(renderMarkdown('<img src=x onerror=alert(1)>'), /<img(?:\s|>)/);

assert.equal(renderMarkdown(raw), renderMarkdown(raw));
assert.match(renderMarkdown('**bold'), /\*\*bold/);
assert.match(renderMarkdown('```python\nprint("hello")'), /<pre data-language="python">/);
