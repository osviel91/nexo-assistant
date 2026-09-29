import assert from 'node:assert/strict';
import { eligibleRerankerModels, rerankerState } from '../web/assets/reranking-state.js';

const providers = [
  { id: 'omlx', name: 'oMLX', models: [{ id: 'Cyber-Tiel', capabilities: [] }, { id: 'reranker-a', capabilities: ['reranking'] }] },
  { id: 'other', name: 'Other', models: [{ id: 'reranker-b', capabilities: ['reranking'] }] },
];

assert.deepEqual(eligibleRerankerModels(providers, 'omlx').map((model) => model.id), ['reranker-a']);
assert.equal(rerankerState(providers, 'omlx', '', false).capabilityAvailable, true);
assert.equal(rerankerState(providers, 'omlx', '', false).configured, false);
assert.equal(rerankerState(providers, 'omlx', '', false).modelDisabled, false);
assert.equal(rerankerState(providers, 'omlx', '', false).enabled, false);
assert.equal(rerankerState(providers, 'omlx', 'reranker-a', false).configured, true);
assert.equal(rerankerState(providers, 'omlx', 'reranker-a', true).enabled, true);
assert.deepEqual(eligibleRerankerModels(providers, 'other').map((model) => model.id), ['reranker-b']);
assert.equal(rerankerState(providers, 'missing', '', false).modelDisabled, true);
assert.equal(rerankerState([{ id: 'empty', models: [{ id: 'Cyber-Tiel', capabilities: [] }] }], 'empty', '', false).capabilityAvailable, false);
assert.equal(rerankerState(providers, 'omlx', 'removed-model', true).enabled, false);
