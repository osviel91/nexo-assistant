import assert from 'node:assert/strict';
import { effectiveMessageIdentity } from '../web/assets/identity.js';

const messages = [
  { role: 'assistant', model_id: 'Cyber-Tiel', runtime: { resolved_model: 'Cyber-Tiel' } },
  { role: 'assistant', model_id: 'Cyber-Tiel', runtime: { agent_profile_name: 'Tiel', resolved_model: 'Cyber-Tiel' } },
  { role: 'assistant', model_id: 'Gemma4-e2b', runtime: { agent_profile_name: 'Gemma2B', resolved_model: 'Gemma4-e2b' } },
];

assert.equal(effectiveMessageIdentity(messages[0]), 'NEXO · Cyber-Tiel');
assert.equal(effectiveMessageIdentity(messages[1]), 'TIEL · Cyber-Tiel');
assert.equal(effectiveMessageIdentity(messages[2]), 'GEMMA2B · Gemma4-e2b');

const reloaded = JSON.parse(JSON.stringify(messages));
assert.equal(effectiveMessageIdentity(reloaded[0]), 'NEXO · Cyber-Tiel');
assert.equal(effectiveMessageIdentity(reloaded[1]), 'TIEL · Cyber-Tiel');
assert.equal(effectiveMessageIdentity(reloaded[2]), 'GEMMA2B · Gemma4-e2b');
