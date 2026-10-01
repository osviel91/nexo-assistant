import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const result = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });
assert.equal(result.status, 0, result.stderr || result.stdout);

const source = readFileSync('web/assets/app.js', 'utf8');
const html = readFileSync('web/index.html', 'utf8');
assert.match(source, /state\.currentNotebookId = null;\n\s*renderNotebookPicker\(\);/);
assert.match(source, /visibleNotebookId !== state\.currentNotebookId/);
assert.match(source, /state\.webEnabled = false;\s*state\.toolsEnabled = false;\s*renderToolToggles\(\);/);
assert.match(source, /data\.thinking_delta[\s\S]*message\.runtime\.thinking\.content \+= data\.thinking_delta/);
assert.match(source, /<details class="thinking-block"><summary>Thinking/);
for (const tab of ['providers', 'general', 'knowledge', 'tools', 'mcp', 'decision']) {
  assert.match(html, new RegExp(`data-settings-tab="${tab}"`));
  assert.match(html, new RegExp(`data-settings-panel="${tab}"`));
}
assert.match(html, /id="mcp-auth-type"/);
assert.match(html, /id="mcp-auth-token" type="password"/);
