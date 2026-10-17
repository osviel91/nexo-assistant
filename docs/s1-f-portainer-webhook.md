# S1-F GHCR latest and Portainer webhook

## Release path

The normal publisher builds once and publishes the immutable
`sha-${GITHUB_SHA}` tag. Its digest remains in `release-contract.json`. A
separate, serialized release job may move `latest` to that exact digest only
after the tests pass, the migration gate is `no-migration-files-changed`, and
`NEXO_PRODUCTION_SCHEMA_VERSION` is exactly `31`. The job checks that its SHA
is still the current `main` tip before changing `latest` and again before
calling the webhook. A migration or unknown schema can publish its SHA image,
but cannot update `latest` or invoke the webhook.

The Portainer workflow uses the S1-F webhook helper, not
`scripts/portainer_deploy.py`. The S1-E API deployer remains available as a
separately reviewed alternative. Qdrant has its own manifest and is not in this
release path.

Portainer's webhook forces an image pull during a Git-stack redeploy. The
Compose therefore remains unchanged and keeps `${NEXO_IMAGE}`. Set the Git
stack's `NEXO_IMAGE` variable to
`ghcr.io/osviel91/nexo-assistant:latest` for normal releases. To pin or roll
back, set that same variable to a previously published
`ghcr.io/osviel91/nexo-assistant@sha256:...` digest. Set it back to `latest`
when resuming automatic releases.

## One-time Portainer preparation — not yet performed

Portainer 2.45's stack webhook handler requires a Git-backed stack. Its Git
settings update path refuses a stack with no existing Git configuration, so
the Web Editor stack cannot be converted in place. The transition must replace
the Portainer stack definition. Schedule and approve a maintenance window
before performing these steps:

1. Confirm the current NEXO stack and endpoint identities. Back up the external
   SQLite volume and securely capture the existing stack environment values;
   never put those values in logs, this repository, or the Git repository.
2. In Portainer Business Edition, configure a Git source for
   `osviel91/nexo-assistant`, branch `main`, Compose path
   `docker-compose.portainer.yml`. Configure read-only Git credentials in
   Portainer if the repository requires them.
3. Preserve every current stack environment variable, changing only
   `NEXO_IMAGE` to `ghcr.io/osviel91/nexo-assistant:latest`. Keep the same
   Docker endpoint and stack/project name.
4. The Compose declares external volume `nexo-assistant_nexo-data` and external
   network `nexo-assistant-shared`. Verify both exist on the same endpoint.
   During replacement, stop and remove only the old stack object/containers;
   explicitly retain the external volume and network. Recreate the Git stack
   with the same Compose and variables, then verify NEXO health and existing
   data before continuing. Do not alter the independent Qdrant stack.
5. Disable periodic Git polling so automatic production changes come only
   through this release workflow. From the verified NEXO Git stack, create a
   **new** stack webhook and store its full URL as the GitHub secret
   `NEXO_PORTAINER_WEBHOOK_URL`. Treat it as a bearer credential; do not reuse
   an older webhook URL.
6. Ensure the GitHub-hosted runner can reach the Portainer webhook over
   validated HTTPS. Portainer's endpoint must also be able to pull the GHCR
   package. No runner or Portainer credentials are currently configured by
   this branch.

The conversion changes the running stack and may change its Portainer stack
ID. Complete and verify it only under the separate production change approval;
this code change does not perform it.

## Cutover and recovery procedure

The Web Editor and Git source cannot own a stack with the same name on the same
endpoint at the same time. The cutover therefore has a short outage:

1. Before the window, verify the NEXO stack and endpoint in Portainer. Record
   the complete Compose text and every stack environment variable in an
   operator-controlled secret store; do not paste values into tickets or logs.
   Record the current `NEXO_IMAGE` digest for rollback.
2. Verify the external volume and network by their exact names. Make and
   validate a consistent backup of the NEXO data volume. Plan a graceful stop
   of NEXO before taking a file-level SQLite backup.
3. Stop the Web Editor stack. Remove its Portainer stack object and containers
   only, explicitly declining any option to remove volumes. Confirm the
   external volume and network still exist before continuing.
4. Create a Git stack on the same endpoint, with the same `nexo-chat` stack
   name, repository/branch/path from above, the exact saved environment, and
   the same Compose file. Initially use the recorded digest as `NEXO_IMAGE`;
   this avoids combining the stack-source conversion with an unverified
   application update.
5. Start the Git stack and perform the verification below. If it passes, set
   its `NEXO_IMAGE` to `latest`, enable only the stack webhook, and verify the
   newly generated URL belongs to this NEXO stack. Do not enable scheduled Git
   polling. The eventual first automatic release occurs only after S1-F is
   merged; this branch does not contact the webhook.

### If the Git stack fails

- Do not remove, recreate, or restore over `nexo-assistant_nexo-data` as a first
  response. Keep the backup and failed stack definition for diagnosis.
- Stop and remove the failed Git stack object/containers, explicitly retaining
  external volumes. Recreate the Web Editor stack from the saved Compose and
  environment, using the recorded pre-cutover digest, and the same endpoint,
  external volume, and external network. This restores the old source mode
  without deleting the SQLite volume.
- Confirm NEXO and Qdrant health. Restore the backup only if the database is
  actually damaged and after verifying the intended source/target; never
  overwrite a healthy current database with an older snapshot.
- If the observed SQLite schema is newer than v31, leave the app stopped and
  escalate for a separate migration/recovery decision. Do not try to recover
  by starting an older image against that database.

## Post-cutover verification

These checks are prepared for the approved cutover; they have not been run
against production:

1. Portainer shows the NEXO Git stack `Active` and its `nexo-chat` container
   running with the recorded digest. Verify the Qdrant stack remains active and
   unchanged in Portainer.
2. From the NEXO container console, query only the SQLite migration ledger in
   read-only mode. This prints the version number, not application data:

   ```sh
   python3 -c 'import os, sqlite3; from pathlib import Path; p=Path(os.getenv("NEXO_DATA_DIR", "/data")) / "nexo.sqlite3"; db=sqlite3.connect(p.as_uri()+"?mode=ro", uri=True); db.execute("PRAGMA query_only=ON"); print(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]); db.close()'
   ```

   Require `31`. The application source currently declares
   `CURRENT_SCHEMA_VERSION = 31`; the repository's synthetic-fixture admin CLI
   intentionally refuses to inspect production databases and is not a
   production verification tool.
3. Request `GET /api/health` through the approved NEXO URL. Require HTTP 200,
   top-level `status: ok`, and `vector_store.status: ok`; with the configured
   Qdrant store this verifies NEXO's Qdrant health check. Confirm Qdrant's
   separate Portainer stack and digest are unchanged.
4. After future webhook releases, a webhook 2xx only means Portainer accepted
   the request: Portainer deploys asynchronously. Confirm stack/container state
   and the requested image digest separately; do not treat the webhook status
   alone as proof of a healthy rollout.

## GitHub configuration

Create these repository variables before release eligibility can pass:

| Name | Required value / use |
| --- | --- |
| `NEXO_PRODUCTION_SCHEMA_VERSION` | `31`; any other or missing value blocks `latest` and webhook |
| `NEXO_PRODUCTION_COMMIT` | A known deployed commit ancestor of `main`; missing or invalid means migration gate `unknown` |

Create the GitHub secret `NEXO_PORTAINER_WEBHOOK_URL` only after verifying the
new webhook belongs to the NEXO Git stack. The URL is passed to the helper
through its environment, with no shell tracing, verbose HTTP output, response
body logging, or URL logging. Branch and pull-request runs do not publish or
invoke the webhook. Integrating the workflow into `main` is the activation
point; do not merge until the Portainer conversion, secret, variables, and
required review are ready.

## Verification limits

The Portainer webhook returns before its asynchronous redeployment finishes.
The GitHub job verifies that Portainer accepted the webhook request; it does
not independently inspect the running container's image digest or prove that
the deployment completed. A later operational check should verify NEXO health
and the running digest. No production webhook call has been made from this
branch.
