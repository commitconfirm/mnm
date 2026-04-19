# Contributing to MNM

## Testing on mnm-test

### Purpose

`mnm-test` is a disposable Ubuntu 24.04 VM on Proxmox used exclusively for clean-install validation. It exists so that changes to the deployment process, bootstrap scripts, and Docker Compose stack can be tested from a true zero state before they reach the README or DEPLOYMENT.md. Every installation test must be reproducible on this VM without relying on any pre-installed state.

This VM must remain snapshottable and throwaway. Do not treat it as persistent infrastructure.

### SSH access from workbench

`mnm-test` is reachable from workbench via Tailscale. A dedicated SSH keypair is configured:

```
Host: mnm-test
Tailscale IP: 100.84.15.7
User: claude
Key: ~/.ssh/mnm_test_ed25519
```

The SSH config entry in `~/.ssh/config` on workbench handles all of this. Connect with:

```bash
ssh mnm-test
```

The `claude` user has passwordless sudo. Verify before running any installation test:

```bash
ssh mnm-test 'sudo -n whoami'   # must return: root
```

### Snapshot-rollback workflow

Before starting any installation test, snapshot the VM in the Proxmox UI so you can roll back cleanly if the test leaves the VM in a bad state.

1. Open Proxmox → select the `mnm-test` VM → Snapshots
2. Take a snapshot named for what you're about to test (e.g., `pre-docker-install`, `pre-mnm-deploy`)
3. Run the test
4. If the test fails or leaves debris, roll back to the snapshot and start clean
5. If the test passes, keep the snapshot as a restore point or delete it before the next test

**TODO:** Automate pre-test snapshots via the Proxmox API from workbench. Until then, this is a manual step before each test session.

### Guardrails

- The `claude` user has sudo on `mnm-test` only. Claude Code never receives Proxmox API credentials or access to the hypervisor itself.
- `mnm-test` is a test target, not a development environment. All code changes happen on workbench; mnm-test only receives the result of `git clone` + deploy.
- Never store real network credentials or `.env` secrets in the repo. The test VM uses a dedicated `.env` with lab-only credentials.
