import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const result = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });
assert.equal(result.status, 0, result.stderr || result.stdout);

const source = readFileSync('web/assets/app.js', 'utf8');
assert.match(source, /state\.currentNotebookId = null;\n\s*renderNotebookPicker\(\);/);
assert.match(source, /visibleNotebookId !== state\.currentNotebookId/);
