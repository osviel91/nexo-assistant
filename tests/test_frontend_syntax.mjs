import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';

const result = spawnSync(process.execPath, ['--check', 'web/assets/app.js'], { encoding: 'utf8' });
assert.equal(result.status, 0, result.stderr || result.stdout);
