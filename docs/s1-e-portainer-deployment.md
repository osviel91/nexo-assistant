# S1-E Portainer API deployer (alternative)

S1-E added `scripts/portainer_deploy.py` and mocked tests for updating the
NEXO-only Docker Compose stack by immutable image digest through the Portainer
API. The helper preserves the stack environment, validates the stack and
endpoint identity, polls deployment state, checks the running container's
`RepoDigests`, and probes NEXO health.

S1-F keeps this deployer and its tests in the repository as an alternative, but
the GitHub workflow does **not** call it and does not require its Portainer API
secrets. The S1-F release path publishes an immutable SHA image, gates the
mutable `latest` alias on migration/schema eligibility, then invokes the
Business Edition stack webhook.

Using the API deployer later would be a separate release design and require its
own explicit review and configuration. No production API update is performed
by this S1-F workflow.
