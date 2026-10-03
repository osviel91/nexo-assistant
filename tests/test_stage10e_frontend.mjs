import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const html = readFileSync('web/index.html', 'utf8');
const tokens = readFileSync('web/assets/tokens.css', 'utf8');
const shell = readFileSync('web/assets/shell.css', 'utf8');
const syntax = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });

assert.equal(syntax.status, 0, syntax.stderr || syntax.stdout);
for (const region of ['data-region="sidebar"', 'data-region="workspace"', 'data-region="header"', 'data-region="conversation"', 'data-region="composer"']) {
  assert.match(html, new RegExp(region));
}
for (const token of ['--color-bg-app', '--color-bg-surface', '--color-text-primary', '--color-border', '--space-1', '--space-8', '--radius-md', '--sidebar-width', '--content-max-width', '--composer-max-width']) {
  assert.match(tokens, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
}
assert.match(shell, /\.icon-button/);
assert.match(shell, /\.primary-button/);
assert.match(shell, /@media \(max-width: 700px\)/);
assert.match(shell, /--color-overlay/);
assert.match(html, /id="retrieval-benchmark-form"/);
assert.ok(html.indexOf('id="retrieval-benchmark-form"') > html.indexOf('id="lab-surface"'));
assert.ok(html.indexOf('id="retrieval-benchmark-form"') < html.indexOf('id="settings-surface"'));
assert.match(html, /id="benchmark-candidates"/);
assert.match(html, /id="benchmark-repetitions"/);
assert.match(html, /Copy results/);
assert.match(html, /Download JSON/);
const app = readFileSync('web/assets/app.js', 'utf8');
assert.match(app, /retrieval-benchmark/);
assert.match(app, /candidate_limit/);
assert.match(app, /candidate_limit: experiment\.candidate_limit/);
assert.match(app, /id: 'benchmark', label: 'Retrieval benchmark'/);
assert.match(app, /benchmark-progress/);
assert.match(app, /createObjectURL/);
assert.match(html, /id="agent-start-choice"/);
for (const field of ['agent-name', 'agent-description', 'agent-instructions', 'agent-provider', 'agent-model', 'agent-notebook-options', 'agent-tool-options', 'agent-max-tool-calls']) {
  assert.match(html, new RegExp(`id="${field}"`));
}
assert.match(app, /Saved bindings are preserved/);
assert.match(app, /agent\.notebook_ids\.length > 1/);
assert.match(app, /tool\.name\.startsWith\('native\.'\)/);
assert.match(app, /state\.toolsEnabled = Boolean\(data\.conversation\.tools_enabled\)/);
assert.doesNotMatch(app, /state\.toolsEnabled = mode === 'agent'/);
assert.match(app, /Unclassified \(blocked\)/);
assert.match(app, /Blocked until classified/);
assert.equal((app.match(/state\.busy = false;\s*\$\('#send-button'\)\.disabled = false;\s*renderMessages\(\);/g) || []).length, 1);
assert.match(app, /approvalBusy: false/);
assert.match(app, /approval\.status === 'pending'.*state\.approvalBusy/);
assert.match(app, /state\.approvalBusy = false;\s*\$\('#send-button'\)\.disabled = state\.busy;\s*renderMessages\(\);/);
