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
assert.match(html, /id="benchmark-candidates"/);
assert.match(html, /id="benchmark-repetitions"/);
assert.match(html, /Copy results/);
assert.match(html, /Download JSON/);
const app = readFileSync('web/assets/app.js', 'utf8');
assert.match(app, /retrieval-benchmark/);
assert.match(app, /candidate_limit/);
assert.match(app, /benchmark-progress/);
assert.match(app, /createObjectURL/);
