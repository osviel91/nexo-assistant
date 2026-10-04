import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const result = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });
assert.equal(result.status, 0, result.stderr || result.stdout);

const source = readFileSync('web/assets/app.js', 'utf8');
const html = readFileSync('web/index.html', 'utf8');
assert.match(source, /state\.currentNotebookId = null;\n\s*renderNotebookPicker\(\);/);
assert.match(source, /visibleNotebookId !== state\.currentNotebookId/);
assert.match(source, /state\.webEnabled = false;\s*state\.toolsEnabled = true;\s*renderToolToggles\(\);/);
assert.match(source, /state\.toolsEnabled = Boolean\(data\.conversation\.tools_enabled\)/);
assert.match(source, /state\.webEnabled = Boolean\(data\.conversation\.web_enabled\)/);
assert.match(source, /renderToolToggles\(\);\s*renderNotebookPicker\(\);/);
assert.match(source, /const field = key === 'webEnabled' \? 'web_enabled' : 'tools_enabled'/);
assert.match(source, /data\.thinking_delta[\s\S]*message\.runtime\.thinking\.content \+= data\.thinking_delta/);
assert.match(source, /dialog\.showModal\(\)/);
assert.match(source, /textContent = interaction\.payload\.title/);
assert.match(source, /api\(`\/interactions\/\$\{encodeURIComponent\(interaction\.id\)\}`/);
assert.match(source, /if \(data\.interaction\) await presentInteraction\(data\.interaction\)/);
assert.match(source, /if \(data\.trace\)[\s\S]*message\.trace\.push/);
assert.match(source, /failureCode = 'stream_interrupted'/);
assert.match(source, /runtime\.diagnostic_error = \{ stage: streamStage, code: failureCode \}/);
assert.match(source, /document\.documentElement\.dataset\.ui === 'developer'[\s\S]*runtime-diagnostic/);
assert.match(source, /\['tool', 'model', 'error_code', 'action', 'policy_decision', 'execution_status'\]\.includes\(key\)/);
assert.match(source, /<details class="thinking-block"><summary>Thinking/);
for (const tab of ['providers', 'general', 'knowledge', 'tools', 'mcp', 'decision']) {
  assert.match(html, new RegExp(`data-settings-tab="${tab}"`));
  assert.match(html, new RegExp(`data-settings-panel="${tab}"`));
}
assert.match(html, /id="mcp-auth-type"/);
assert.match(html, /id="mcp-auth-token" type="password"/);
