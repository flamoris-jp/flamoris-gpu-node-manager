# Updater compatibility (1.2.0)

Source adoption is under review. Version metadata does not certify a published
release or a real-host update. No production data, credentials or service is
changed by this PR.

`flamoris-gpu-node-manager-update-owner --config /protected/owner.json` serves the application's
separate bounded mTLS Owner endpoint. Its schema/resource validation lives in
`flamoris_gpu_node_manager.updater`. Updater Web is independent of Studio.

Applications install the MCP-independent SDK pinned to an immutable Updater
source commit. Do not install the full Updater distribution into this environment.
Managed deployments set `FLAMORIS_UPDATE_REQUIRED=1` and
`FLAMORIS_UPDATE_STATE=/private/owner-state`; missing state fails closed.
Accepted work is durable across processes. Unknown/cancelled work remains a
blocker across restart; no timer, PID disappearance or reconnect clears it.

The independent entry CLI plans and verifies existing schemas, backup and an
isolated restore before target activation. It does not initialize databases,
erase data or auto-reconcile uncertain requests. Legacy services/external writers
must be stopped through their existing authorized maintenance procedure.

Read the [complete application entry contract](https://github.com/flamoris-jp/flamoris-updater/blob/feat/application-entry-v1/docs/APPLICATION_ENTRY.md)
for required resource classes, Owner/Helper/Entry configuration fields, restore
isolation, fixed lifecycle bindings and failure handling. Private profiles and
signed CI-built artifacts are required; source compatibility is not operational
acceptance. Preserve all current data, grants, configuration identities and
independently readable history.

This remains native. Preserve the installed deployment overlay, environment and
matched dependencies. Updater uses RuntimeManager and its existing host-wide lock.
Evidence identities/locks are preserved; failed inspection never proves OFF.
The v1.1 baseline tag identifies source whose package reports 1.0.0. The entry
release is 1.2.0; do not infer actual installed identity from that tag.
