import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const source = readFileSync('web/assets/app.js', 'utf8');
const html = readFileSync('web/index.html', 'utf8');
const css = readFileSync('web/assets/app.css', 'utf8');

const syntax = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });
assert.equal(syntax.status, 0, syntax.stderr || syntax.stdout);

assert.match(source, /copyText\(message\.content \|\| ''\)/);
assert.match(source, /document\.execCommand\('copy'\)/);
assert.match(source, /formatRunDiagnostics\(state\.lastRuntime\)/);
assert.match(source, /state\.lastRuntime = state\.messages\.find/);
assert.match(source, /dense_candidate_count/);
assert.match(source, /relevance_gate_status/);
assert.match(source, /generation_duration_ms/);
assert.match(source, /state\.streamTimestamps\.first_answer_delta/);
assert.match(source, /type: 'RESPOND'/);
assert.match(source, /state\.followingBottom/);
assert.match(source, /scrollToBottom\(\)/);
assert.match(source, /aria-label="Copy answer"/);
assert.match(source, /aria-label="Branch from message"/);
assert.match(source, /aria-label="Show run details"/);
assert.match(source, /branchFrom\(button\.dataset\.branchMessage\)/);
assert.match(source, /notebook-sources/);
const diagnostics = source.slice(source.indexOf('function formatRunDiagnostics'), source.indexOf('function moduleEnabled'));
assert.doesNotMatch(diagnostics, /api_key|thinking_content|system_instructions|embedding|source\.content/);
assert.match(html, /id="copy-diagnostics"/);
assert.match(html, /id="scroll-bottom"/);
assert.match(css, /\.message-toolbar \.icon-button/);
