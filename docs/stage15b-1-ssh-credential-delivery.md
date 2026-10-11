# NEXO Stage 15B.1: SSH Credential Delivery

- Estado: feasibility and isolated SSH prototype completed; awaiting architecture/security review
- Fecha: 2026-10-11
- Base: `74513730e0712f6925f0106052bd6a50e78e967c`
- OAuth and credential values used in tests: synthetic only
- Scope exclusions: real SIWC login, Responses API, Codex app-server, NEXO runtime/auth, providers, SQLite, migrations, deployment

## Decision

**GO for a future host-level SSH delivery implementation, conditional on independent review and NEXO credential storage/auth decisions. NO-GO for production use now.** A real OpenSSH transfer was exercised in a disposable local Docker container. The test proves the selected SSH setup and receiver behavior in that isolated environment. It does not validate the production Docker host, its SSH configuration, user provisioning, key enrollment, Portainer operations, NEXO authorization or credential persistence.

Use the already-authorized SSH service on the Docker host, not an SSH daemon in the NEXO container and not a public NEXO import endpoint. A dedicated OS account/key is an explicit credential-import capability, not proof of a NEXO application user or administrator. Keep the import staged and require an authorized NEXO-side confirmation before activation once NEXO has an authentication model. Current NEXO has no such model; do not transfer real credentials to it.

## Official and OpenSSH sources

OpenAI SIWC [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) says to complete OAuth locally, transfer the protected credential record to the VM over a secure channel such as SSH, preserve the VM's host ID, and let the VM refresh later. It does not require SSH inside the application container or prescribe a NEXO receiver protocol. Follow the SIWC constraints in [Stage 15A](stage15a-chatgpt-oauth-feasibility.md): the local helper owns loopback callback, state/nonce/PKCE and token/identity/scope validation; the VM uses its stable `ext_agent_host_id`; no Codex credential extraction.

OpenSSH references consulted:

- [OpenSSH `ssh_config(5)`](https://man.openbsd.org/ssh_config): `StrictHostKeyChecking`, `UserKnownHostsFile`, `IdentityFile`, `IdentitiesOnly`, `IdentityAgent`, `BatchMode`, forwarding and TTY controls.
- [OpenSSH `sshd_config(5)`](https://man.openbsd.org/sshd_config): `ForceCommand`, `AllowUsers`, `AuthenticationMethods`, `DisableForwarding`, `PermitTTY`, `PermitUserRC`, `AuthorizedKeysFile` and listener configuration.
- [OpenSSH `sshd(8)` authorized_keys format](https://man.openbsd.org/sshd): `restrict` disables forwarding, PTY and user rc for that key; a forced command constrains the session command.

Pin the operator-approved host public key/fingerprint through an authenticated out-of-band channel or a trusted SSH host CA. `ssh-keyscan` may collect a key but does not authenticate it; never trust its output as the initial pin by itself.

## Proposed architecture

```text
Mac helper (OAuth client)                   Docker host (existing sshd)
  1. official SIWC loopback OAuth              host key pinned out of band
  2. validate state/PKCE/OIDC/scopes            dedicated OS user: nexo-import
  3. build bounded synthetic/real bundle        public key auth only
  4. ssh strict host check + stdin  ----------> forced receiver, no shell
     no token in argv/temp file                  validate/consume transaction
                                                stage outside NEXO runtime
                                                redacted receipt on stdout
                                                       |
                                                       | future, authenticated local handoff
                                                       v
                                             NEXO credential manager
                                             (not implemented; fail closed)
```

The SSH layer establishes two distinct facts: the client authenticates the server's host key; the server authenticates the helper's SSH user key. Neither alone identifies a NEXO user or authorizes account activation. For a real deployment, the operator must deliberately enroll a helper key to the dedicated receiver account, and a separate application authorization must associate the staged account with an authenticated NEXO principal. The receiver process has its own constrained OS identity and must not run as root or have general Docker-socket access.

### Receiver contract in the prototype

- Transport: OpenSSH exec session only. Helper invokes `ssh` with an argument vector, `-F /dev/null`, exact IP/port, a dedicated known-hosts file, `StrictHostKeyChecking=yes`, `BatchMode=yes`, one explicit identity, agent disabled, no PTY, and forwarding disabled. Payload bytes are sent through stdin; the requested command is ignored by server-side `ForceCommand`.
- Server: dedicated `nexo-import` Unix account in an isolated image, public-key authentication only, `AllowUsers nexo-import`, no password/keyboard-interactive/root login, no PTY, no user rc, no TCP/agent/stream-local/X11/tunnel forwarding, `DisableForwarding yes`, fixed receiver command.
- Key scope: prototype public key uses `restrict`; no shell capability is intended. In production, enroll a dedicated key only for this function. Do not reuse a broad human administrator key. Keep private key material in the user's approved macOS keychain/hardware-backed SSH mechanism where practical; never forward an agent to the remote host.
- Request: one JSON object with exactly `version`, `transaction_id`, `expires_at`, `credentials`. Version is 1; transaction is a UUID matching an independently provisioned pending transaction; expiry must be in the future and no more than 300 seconds away; aggregate request is at most 16 KiB. Credentials require non-empty string `client_id`, `access_token`, `refresh_token`; optional fields are restricted to a small SIWC metadata allowlist and values are bounded to 8192 characters. Duplicate JSON keys, unknown fields, expired transaction, wrong transaction and malformed input are rejected.
- Replay: only after all validation succeeds, create a SHA-256-named marker with `O_CREAT|O_EXCL`; a second delivery for the transaction is rejected. Marker contains only `consumed`, not credential material. A production implementation needs durable, atomic single-consume state across restarts/workers and a deliberate cleanup policy; the test marker lives only in the disposable container filesystem.
- Receipt: success returns only `status` and `transaction_id`; rejection returns a generic status/reason and never echoes input. SSH daemon logs contain connection/account and key-fingerprint metadata, not stdin. Do not enable debug-level logging in production. No credential file, temporary credential file, URL parameter, or process argument is used.
- Confirmation boundary: the receipt means validated bytes reached and were accepted by the test receiver. It is not proof of NEXO-side storage, account ownership, operator confirmation, provider activation or successful token validation. A real receiver must keep the payload staged and inaccessible to inference until a separately authenticated NEXO admin confirms it.

Maximum payload bounds protect the receiver from accidental/oversized input, not all resource exhaustion. Production also needs SSH connection limits, request timeout, rate limiting, disk/inode quotas for non-secret markers and monitoring that excludes payloads.

## Alternatives

| Mechanism | Server/client identity | Secret exposure and constraints | Complexity / Docker fit | Decision |
| --- | --- | --- | --- | --- |
| SSH exec with dedicated key, forced command and stdin | Pinned SSH host key/CA; dedicated SSH public key maps to one restricted OS account | Payload is encrypted/integrity-protected in SSH transport; not in argv, URL or a credential file. Receiver must bound and validate bytes; SSH auth is not NEXO user auth. | Low/moderate. Uses host's existing `sshd`; no daemon or port in NEXO container. Receiver can stage to host-owned protected location. | **Preferred.** |
| SSH stdin to an unrestricted shell command | SSH authenticates endpoints, but account can run arbitrary commands | No local temp file required, but command quoting, shell interpretation, logs and shell/account privileges enlarge exposure. | Superficially simple; hard to constrain correctly and easy to misuse. | Reject. Use forced receiver instead. |
| SSH local tunnel to loopback-only host receiver | SSH host and client keys authenticate tunnel; loopback listener is not public | SSH protects tunnel. Receiver must still authenticate/authorize application messages or rely on the SSH-bound tunnel, validate origin, size and replay. Local hostile processes may race/attach to a loopback port; use a Unix socket or random local port and short lifetime. | Medium/high: second protocol/server, forwarding-specific SSH capability and lifecycle. Receiver must live on host or suitable host network, not NEXO container. | Viable fallback if an interactive staged-confirmation protocol is required; not needed for a one-shot upload. |
| SCP/SFTP or `rsync` over SSH | SSH host/client authentication | Transfer is encrypted but creates a credential file at rest, requiring atomic permissions, secure deletion limitations and import cleanup. | Low/moderate; receiver account must be jailed and restricted to a single landing directory. | Not preferred because the requested prototype should avoid temporary credential files. |
| HTTPS public import endpoint | TLS server identity; needs application user authentication and authorization | TLS alone does not identify the authorized NEXO principal; endpoint/body/logging/replay risks remain. | Would require public API surface and NEXO auth, currently absent. | Reject for this stage. |

SSH forwarding keys are not equivalent to forced-command keys. If the tunnel alternative is selected, use a separate key/account policy limited to the exact loopback `PermitOpen` destination and disable shell/other forwarding; do not weaken the one-shot receiver key to make a tunnel work.

## Threat model and trust boundaries

| Asset/threat | Required control | Prototype evidence / residual risk |
| --- | --- | --- |
| Wrong/impersonated host | Verify pinned host public key or host CA with strict checking; exact host alias, port and known-hosts scope; fail closed on unknown/changed key | Integration test supplied a deliberately wrong host key and SSH refused the session. The test pin was fetched via Docker exec control plane, not a deployment procedure. Production enrollment ceremony remains undefined. |
| Unauthorized helper/client | SSH public-key auth; only explicitly enrolled key; dedicated OS account; key restrictions and server `ForceCommand`; no password | Integration test with a second unlisted SSH key was rejected. This proves test `authorized_keys` behavior, not production operator provisioning. A stolen enrolled key can submit data within its receiver capability until revoked. |
| Unauthorized NEXO user/admin | Do not infer NEXO authority from the SSH account. Bind import to authenticated application principal and require separate confirmation before activation | NEXO currently has no general authn/authz; not implemented or tested. This is the main production blocker. |
| Receiver compromise/privilege escalation | Dedicated unprivileged UID, root-owned code/config, no shell/TTY/forwarding, bounded input, minimal filesystem permissions, no Docker socket | The isolated receiver runs as `nexo-import` with global forced command and forwarding disabled. No chroot, production OS hardening, sandbox profile or host-to-NEXO handoff is tested. |
| Replay or racing transfers | Server-issued single-use transaction, short expiry, durable atomic consume, reject duplicates | Same transaction was replayed over actual SSH and rejected; unit tests also cover expiry, wrong transaction and marker exclusivity. Multi-process/restart behavior was not tested. |
| Malformed/oversized payload | Strict version/schema/allowlist, duplicate-key rejection, max bytes and field lengths, no echo | Unit tests reject malformed JSON, duplicate/unknown fields, expiry, wrong transaction and >16 KiB. The actual SSH run exercised malformed and oversized inputs. |
| Secret leakage in argv, files, logs, receipt | stdin only; no shell interpolation; no credential temp files; redacted receipt/errors; server logs never include request body | Synthetic token strings were absent in client output, daemon logs and files in the test temp directory. This is scoped to this code/path; OS audit, crash reports, terminal capture and production logging are not covered. |
| Network MITM | SSH cryptographic host-key verification; strict pin/CA trust ceremony; refuse TOFU/unknown key | Strict checking and wrong-key rejection tested. SSH encryption without a trusted host-key source would not satisfy identity verification. |
| Host compromise or malicious root | SSH does not protect credentials after decryption on the host | Out of scope for transport prototype; production requires host access controls, encrypted storage, backups/secret handling and incident response. |
| DNS/IP destination substitution | Pin identity to exact host key and intended user/port; no shell or proxy-command from ambient user config | Prototype accepts an IP literal only, uses `-F /dev/null` and isolated known_hosts; rejects DNS names and wrong pinned host key. Production hostname/SSH CA policy needs explicit design. |

The public SSH port is an existing host service and still requires normal patching, firewalling, rate limits, key revocation and monitoring. Do not expose a new NEXO import port. `sshd` transport encryption protects data in transit; it does not encrypt storage, authenticate a NEXO principal or make the host trustworthy.

## macOS and Docker host requirements

**macOS helper, future phase:** run OAuth in a local process and bind callback only to `127.0.0.1`; validate state/PKCE/OIDC/scopes before building the transfer record. Ask the user to choose the host alias, SSH user and approved identity; show the pinned host fingerprint for an explicit verification ceremony. Use an argument vector, never a shell-composed command. Keep OAuth material in process memory until send completes; send it only on stdin. No browser storage, URL, clipboard, terminal output, shell history, temp bundle or debug dump. A network/SSH failure leaves the credential on the Mac for retry or explicit discard; never print it.

**Docker/Portainer host:** SSH terminates on the host's already managed SSH daemon. Provision one dedicated OS import account and key out-of-band; do not assume sshd exists in NEXO, install it in the app container, or give the receiver access to `/var/run/docker.sock`. Receiver and key authorization should be host-managed, auditable and revocable. Any handoff to the NEXO container should use an explicit local mechanism with minimal permissions, not a new public listener or broad volume mount. Portainer deployment/restart must preserve host ID and external encryption-key provisioning; pending transactions should expire or fail closed on reboot.

The PoC deliberately uses an ephemeral Docker image, random host/client keys in a temporary directory/container, port mapped only to `127.0.0.1`, and removes the container and image tag after the test. The synthetic SSH client private key is briefly a temporary file because OpenSSH reads the `-i` identity by path; it is not an OAuth credential and the temporary directory is removed at test exit. The scans assert that the synthetic OAuth access/refresh token values are absent from all these files, logs and process outputs. The PoC does not edit `~/.ssh/config`, `~/.ssh/known_hosts`, macOS sshd, server sshd, firewall, Docker Compose or Portainer configuration.

## Storage and credential lifecycle (design only)

- Transport: SSH encryption and endpoint authentication end when the session closes. Stdin avoids an intermediate credential file but the receiver necessarily holds plaintext in process memory after SSH decrypts it.
- Stage: a future receiver may hold the validated bundle briefly in protected memory or an encrypted, owner-only staging area if confirmation crosses process boundaries. Never place it in an ordinary temp file, container logs, general-purpose volume or image layer. Expire and securely remove staging state; consider crash dumps/swap and backup behavior.
- Persist: after separate authorized confirmation, store access/refresh/id tokens as an authenticated-encryption envelope. Encryption key must be supplied from an external secret manager/operator secret, not SQLite, the same volume, image, or database backup. Bind ciphertext to provider-connection identity and schema/version as AEAD associated data. Fail closed if key is missing/invalid; never fall back to plaintext.
- Rotate key: document re-encryption/backup restore before key rotation. Backup the key separately under operator-controlled recovery policy; losing it means reauthorization, not plaintext recovery.
- Refresh: refresh near expiry using the issued client ID and documented SIWC resource; serialize by account, update access token/expiry/scopes/rotated refresh token atomically, and never log request/response bodies. Invalid/revoked refresh fails closed and requires reauthorization.
- Disconnect/revocation: stop provider use first, attempt the provider's documented refresh-token revocation endpoint, erase local credentials after the attempt, and report if remote revocation was not confirmed. User can revoke from ChatGPT Settings. Transferred VM sessions currently lack host-specific usage attribution/revocation according to SIWC docs.
- Restart: retain only stable host ID and encrypted credential state across restart. Expire pending transfer/confirmation; after key loss, mark unavailable and require reauthorization. None of these storage/lifecycle mechanisms are implemented by this prototype.

## Prototype and verified results

Files added for Stage 15B.1:

- `prototypes/ssh_delivery/client.py`: strict `ssh` argument vector and stdin delivery.
- `prototypes/ssh_delivery/receiver.py`: bounded parser, allowlist, expiry and atomic single-use marker; receipt/errors redact data.
- `prototypes/ssh_delivery/Dockerfile`, `sshd_config`, `entrypoint.sh`: isolated test-only OpenSSH receiver image and forced-command account.
- `tests/test_stage15b1_ssh_delivery.py`: four receiver unit tests and one opt-in real OpenSSH/Docker integration test. The integration test runs only with `NEXO_RUN_SSH_POC=1` and Docker present; normal `./scripts/nexo check` does not create containers.

Command `NEXO_RUN_SSH_POC=1 python3 -m unittest tests.test_stage15b1_ssh_delivery -v`: **5 tests passed**, including the real SSH integration. The integration verified:

- Successful SSH delivery with a pinned ephemeral host key, dedicated client key, a forced receiver and payload on stdin.
- Rejection of a different/untrusted host key and an unlisted SSH client key.
- Rejection of hostname destination outside the prototype IP-only policy, unauthorized transaction, replay, invalid JSON and over-limit payload.
- Safe acknowledgment without synthetic token values in stdout/stderr; no synthetic values found in daemon logs or local temp files.
- Container bound only to loopback and cleaned up after the test. The test does not assert that Docker internals, kernel crash data or machine-wide forensic artifacts can never retain process memory.

Two initial integration attempts failed during harness setup: first the test account was locked by Alpine's account defaults, then the receiver script was invoked without a Python shebang. Both were corrected in the isolated prototype; the final focused run passed. These were setup failures, not intermittent passes. The separate pre-existing Stage 15B artifacts remain unchanged.

## Limitations and remaining risks

- This is not SIWC OAuth code and no real credentials or network login were used.
- The container is a test stand-in, not the production Docker host. The Docker control plane supplied the temporary server public key out-of-band for test pinning; production trust bootstrap must be separately reviewed.
- The receiver accepts one transaction ID provisioned into an ephemeral file and writes a non-secret replay marker. Production needs a real authenticated transaction-creation/confirmation authority, concurrency-safe durable replay state and cleanup.
- The successful acknowledgment confirms receiver validation only. No NEXO-side confirmation, provider activation, user ownership check, token validation, storage or refresh was exercised.
- OpenSSH verbose test logs include remote account, source address and client public-key fingerprint. They did not include payloads; production should use normal/quiet logging and appropriate audit retention. Do not enable DEBUG logging for this flow.
- A compromised Docker host/root can inspect process memory after receipt; SSH cannot mitigate that. Transport security and at-rest encryption are separate controls.
- SSH user access may grant more host capability than NEXO should. Dedicated account, forced command and OS isolation reduce the surface but require host administrator review and key revocation operations.
- Exactly-once behavior across host restart and lost acknowledgment needs durable transaction semantics. The PoC's marker survives only for the lifetime of the ephemeral container.
- The localhost-only test demonstrates OpenSSH identity checks but not NAT, dynamic DNS, host key rotation, bastion/ProxyJump, private SSH CA, home-router exposure, firewalling, Portainer, or macOS Keychain UX.

## Recommendation

Proceed only to a reviewed implementation plan. Prefer **SSH exec + stdin + dedicated host OS user + pinned host key + key-restricted forced command**. Keep SSH on the host and stage data outside NEXO until an authenticated NEXO authorization/confirmation model and encrypted credential lifecycle exist. No production SSH configuration or service should be changed as part of this stage. Stage 15C remains blocked; no commits, push or deployment are authorized.

## Verification record

Verification completed:

- `NEXO_RUN_SSH_POC=1 python3 -m unittest tests.test_stage15b1_ssh_delivery -v` -> 5 passed, including the real SSH integration.
- `./scripts/nexo check` -> attempted three times; all failed only at the pre-existing `tests/test_tool_selection.py::ToolSelectionTests::test_catalog_scaling_benchmark`. Each run reported 251 passed, 1 skipped (expected opt-in Docker integration), 1 failed. p95 for 500 tools: 243.44 ms, 396.56 ms, and 188.61 ms (threshold 100 ms). No unrelated code/test was changed. Unrelated Docker workloads and Vitest processes were active on the shared machine; contention is a plausible cause, not a proven diagnosis.
- `./scripts/nexo test-frontend` -> passed independently; the full `check` stopped before reaching frontend tests because of the backend benchmark failure.
- `git diff --check` -> passed. `git status --short --branch` remains on `main` at the requested HEAD and shows only existing Stage 15B untracked files plus files for this stage.
- Seven files added for Stage 15B.1: this document, five files under `prototypes/ssh_delivery/`, and `tests/test_stage15b1_ssh_delivery.py`. The pre-existing Stage 15B untracked files were preserved and not edited in this stage. No commit, push or deployment.
