# S1-E Portainer deployment checkpoint

## Disabled state

The `deploy` job in `.github/workflows/quality.yml` contains the literal condition
`false && ...`. It cannot run on a push or merge, and the workflow dispatch input
defaults to `false`. The deploy job remains skipped until a separately reviewed
code change removes the hard lock. No production credentials or deployment
values belong in the repository.

Manual dispatch also requires the `main` branch, a true
`confirm_production_deploy` input, `migration_gate=no-migration-files-changed`,
and `deployment_eligible=true`. Publication reuses the digest output produced by
the GHCR build job.

## GitHub configuration required before future activation

Create a protected GitHub Environment named `nexo-production`, with required
reviewers, and configure:

| Kind | Name | Purpose |
| --- | --- | --- |
| Secret | `PORTAINER_URL` | HTTPS base URL for the Portainer API; may include a base path |
| Secret | `PORTAINER_API_KEY` | New Portainer API key with only the stack and endpoint permissions needed |
| Variable | `NEXO_PORTAINER_ENDPOINT_ID` | Exact authorized Docker endpoint ID; the stack response must match it |
| Variable | `NEXO_HEALTHCHECK_URL` | HTTPS URL ending in `/api/health`, reachable from the deployment runner |

Create these **repository variables** because the check and publish jobs do not
use the production environment:

| Name | Purpose |
| --- | --- |
| `NEXO_PRODUCTION_SCHEMA_VERSION` | Currently `31`; eligibility is false for any other or missing value |
| `NEXO_PRODUCTION_COMMIT` | Exact commit currently deployed to production; missing, invalid, or non-ancestor values make the migration gate `unknown` |

The target stack ID is fixed to `30` in the workflow. The deployer additionally
requires stack name `nexo-chat`, Docker Compose stack type, the configured
endpoint ID, and a Compose file with only the expected NEXO image interpolation.
Qdrant is not a target. Keep the endpoint variable limited to the approved
Portainer environment.

After a successful deployment, update `NEXO_PRODUCTION_COMMIT` to the deployed
commit. This makes migration classification cover every change since the last
recorded production release instead of only the latest push.

## Runner networking

The runner must reach both `PORTAINER_URL` and `NEXO_HEALTHCHECK_URL` over
validated HTTPS. A GitHub-hosted runner does not automatically have access to
private LAN endpoints. If either address is private, use a controlled
self-hosted runner with the required routes or an explicitly reviewed secure
network path; do not expose Portainer or NEXO just for this job. The Portainer
API key also needs permission to inspect the endpoint's Docker containers and
images through Portainer's endpoint Docker proxy. The target Docker endpoint
must already be authorized to pull the GHCR package; this workflow does not
send registry credentials to Portainer.

The application health endpoint must return HTTP 200 with `status: ok` and
`vector_store.status: ok`. Health response bodies and Portainer environment
values are never logged by the deployer.

## Runtime verification and activation procedure

After Portainer reports stack status `Active`, the deployer queries the Docker
API proxy for the `nexo-chat` Compose project/service, requires exactly one
running container, verifies its configured image reference, and checks that
the inspected image's `RepoDigests` contains the exact GHCR digest from the
publish job. It then requires the NEXO health check to pass. Status `Active`
alone is not treated as success.

GitHub concurrency serializes the production deployment job without cancelling
an in-progress update. Immediately before the API update, the job also checks
that its commit is still the current `main` tip and descends from the recorded
production commit. An older manual dispatch therefore fails closed.

Future activation requires a reviewed change to the literal `false` condition,
provisioning and verifying the GitHub configuration above, and an explicit
`workflow_dispatch` on `main` with `confirm_production_deploy=true`. Merging the
activation change alone will not deploy because the deploy job remains limited
to the manual-dispatch event. Do not activate until runner connectivity,
endpoint identity, production commit/schema values, and required reviewers are
confirmed.
