# Upgrading MNM

This document describes the canonical procedure for updating an MNM
install. It's written for the operator who runs `docker compose` day
to day but doesn't necessarily live in Docker internals.

> **Audience:** network engineers running MNM on their own Docker
> host. The procedure assumes you can run `docker compose up -d`
> and read terminal output; it does not assume you know Docker's
> image-layer caching or Compose's `.env`-reload semantics (which
> we cover below).

## Before you update

1. **Take a host-level snapshot.** If MNM runs in a VM (Proxmox,
   ESXi, vSphere, Hyper-V, AWS EC2, etc.), take a snapshot of the
   VM before updating. This is the fastest rollback path if
   anything goes wrong.

2. **If you can't snapshot, back up the database volume at minimum.**
   MNM's persistent state lives in the `mnm-postgres` service's
   volume (both Nautobot's data and the controller's `mnm_controller`
   database share this volume).

   ```bash
   docker compose exec postgres pg_dumpall -U nautobot > mnm-backup-$(date -u +%Y%m%d).sql
   ```

   Store the dump somewhere outside the install directory.

3. **Confirm working tree clean.** The install lives in a git
   clone. Any local edits will conflict with `git pull`.

   ```bash
   cd /path/to/mnm
   git status
   ```

   If `git status` shows modified files, decide whether to discard
   (`git restore <file>`), stash (`git stash`), or commit them
   before pulling. Local modifications on a production install
   warrant investigation — what changed and why?

4. **Identify the target tag.** Pin to a tagged release rather
   than tracking `main`; tags are the stable deployment surface.

   ```bash
   git fetch --tags
   git tag -l 'v*' --sort=-v:refname | head -5
   ```

   The most recent tag is the recommended target. The full
   release list with notes is at
   https://github.com/commitconfirm/mnm/releases.

5. **Review `CHANGELOG.md` for breaking changes or new env vars.**
   Skim the `## [Unreleased]` and most recent version sections.
   Any new variables in `.env.example` may need adding to your
   `.env` (defaults preserve existing behavior, but you may want
   to set values explicitly).

## Standard update procedure

Four steps. Run each in order; check the verification commands
after each one if you want to debug as you go.

### 1. Pull the update

**Recommended — pin to a tagged release** (this is the production
deployment flow):

```bash
cd /path/to/mnm
git fetch --tags
git checkout v1.0.x          # substitute the actual target tag
```

Tags are the stable deployment surface. `main` may contain
post-release fixes not yet bundled into a tag; production
stability requires pinning to a known release.

*Development hosts only* — tracking `main` HEAD is acceptable if
you understand what you're opting into:

```bash
git fetch
git pull --ff-only
```

`--ff-only` refuses to create a merge commit; if the pull isn't a
fast-forward (e.g., someone committed locally on the install),
the pull fails loudly and you can investigate before anything
changes. Use this flow for a development host you're using to
test pre-release work; do not use it on a production install.

### 2. Review `.env` for new variables

```bash
diff <(grep -E '^[A-Z_]+=' .env.example | cut -d= -f1 | sort) \
     <(grep -E '^[A-Z_]+=' .env | cut -d= -f1 | sort)
```

This compares variable *names* only (not values, which would leak
secrets). Any name in `.env.example` but not in `.env` is a new
variable. Decide whether to add it (with the `.env.example`
default, or a tuned value) or leave it unset (the controller
uses the documented default).

### 3. Rebuild changed services

```bash
docker compose build
```

This rebuilds all services. To rebuild only specific services
when the `CHANGELOG` indicates a narrow change:

```bash
docker compose build controller        # controller-only change
docker compose build nautobot          # nautobot-plugin change
```

### 4. Recreate containers

```bash
docker compose up -d
```

When you've added or modified `.env` variables, use
`--force-recreate` for the affected services:

```bash
docker compose up -d --force-recreate controller
```

> **Why `--force-recreate` matters for `.env` changes:**
> Docker Compose reads `.env` and injects values into containers
> only at container **creation** time, not at process startup.
> Running `docker compose restart <service>` after editing `.env`
> does **not** pick up the new values. This is a footgun
> documented in CLAUDE.md and the `.env` file header.

## Verifying the update

After step 4, confirm the update is healthy.

### All services running

```bash
docker compose ps
```

Every service should show `running` and (where healthchecks are
defined) `healthy`. Anything in `restarting` or `unhealthy`
indicates a problem — check the service's logs.

### Controller has no startup errors

```bash
docker compose logs controller --since 2m | grep -iE "ImportError|Traceback|FATAL"
```

This should return nothing on a successful update. Any match
indicates the controller didn't start cleanly.

### Controller healthcheck is green

```bash
docker inspect mnm-controller --format '{{.State.Health.Status}}'
```

Returns `healthy` on success. If it returns `starting`, wait 30s
and retry — the controller takes a few seconds after boot to come
fully online. If it returns `unhealthy`, check the controller
logs.

### Polling cycle completes without regressions

```bash
docker compose logs controller -f
```

Watch for one polling interval (the default `MNM_POLL_CHECK_INTERVAL`
is 30 seconds — set it lower temporarily if you want faster
feedback). You should see structured log events like
`arp_snmp_collect_complete`, `mac_snmp_collect_complete`,
`lldp_snmp_collect_complete` for each onboarded device, with no
accompanying errors.

Press Ctrl-C to stop tailing.

## Rolling back

If the update has problems and you need to revert:

### Code rollback

**Recommended — roll back to the previous tag:**

```bash
git tag -l 'v*' --sort=-v:refname | head -5    # list recent tags
git checkout v1.0.x                            # the previous known-good tag
docker compose build
docker compose up -d
```

If you're not on a tag (development host tracking `main`) or the
previous good ref isn't tagged, roll back to a SHA instead:

```bash
git log --oneline -10                          # find the previous known-good SHA
git checkout <previous-sha>
docker compose build
docker compose up -d
```

The data layer (Postgres, Prometheus TSDB) is **forward-compatible
within v1.0.x** — Block C polling schema is stable and Nautobot
handles its own migrations. No downgrade migrations are needed
for v1.0.x patch-level rollbacks.

### Full rollback (if you took a snapshot)

The host-level snapshot from "Before you update" is the
belt-and-suspenders option. Restoring the VM from the snapshot
puts everything — code, data, configuration — back exactly as it
was. Slower than a `git checkout`, but covers cases the code
rollback can't (e.g., if you ran an out-of-band command that
modified data).

## Troubleshooting

### `git pull` fails due to local modifications

Cause: a file in the install directory was edited locally.

```bash
git status                # see what changed
git stash                 # save the changes
git pull --ff-only
# decide whether to reapply (git stash pop) or discard (git stash drop)
```

Local modifications on a production install are unusual — figure
out what changed and why before reapplying.

### Image build fails due to disk space

```bash
docker system df          # see usage
docker system prune       # reclaim space (safe — doesn't touch volumes)
```

`docker system prune` removes dangling images, stopped containers,
and unused networks. It does **not** touch named volumes or
running containers, so it's safe to run on a production host. Add
`-a` (`docker system prune -a`) to also remove images that aren't
currently in use, which reclaims more space at the cost of having
to re-pull anything you might want later.

### Healthcheck stays `unhealthy` after recreate

```bash
docker compose logs <service> --since 5m
```

Common patterns in the logs:

- **`KeyError: 'SOMETHING'` or `ValueError: invalid literal for int()`** —
  an env-var problem. Re-check `.env` against `.env.example`.
- **Connection refused** to another service (postgres / nautobot /
  redis) — that dependency hasn't come up yet. Wait 30s and check
  again; if it persists, the dependency itself is unhealthy.
- **`ImportError` or `Traceback` on a fresh codebase** — code
  problem. Roll back (`git checkout <previous-tag>`) to confirm
  the issue is from the update, then file a bug against the
  repository.

If you can't diagnose quickly, rolling back is the fast escape
hatch.

## Update frequency

Treat `CHANGELOG.md` entries as the update signal:

- `[security]` and `[bugfix]` warrant prompt updates — these are
  the changes that fix real problems your install might be
  hitting.
- `[feature]` and `[chore]` can wait for a convenient maintenance
  window.

Subscribe to GitHub releases on the repository
(https://github.com/commitconfirm/mnm/releases) to get a
notification on every new tag.

## Worked example — this update

Updating to the version that introduced this `UPGRADE.md`:

- **Identify the tag.** This update ships in the next v1.0.x tag
  (`v1.0.1` once cut from `main`; substitute the actual tag name
  shown by `git tag -l 'v*' --sort=-v:refname | head -5`).

- **Step 1, pull the update — pin to the tag:**

  ```bash
  git fetch --tags
  git checkout v1.0.x        # substitute the actual tag
  ```

- **New env vars** (Step 2): `MNM_SNMP_TIMEOUT_SEC` (default 10.0)
  and `MNM_SNMP_RETRIES` (default 1). Both are optional; defaults
  preserve existing polling behavior. Step 2 is a no-op unless you
  have devices with known-slow SNMP agents and want to tune.

- **Change scope** (Step 3): controller only. Narrow the rebuild:

  ```bash
  docker compose build controller
  ```

- **Recreate** (Step 4): if you added the new env vars to `.env`,
  use `--force-recreate`. Otherwise plain `up -d` is fine:

  ```bash
  docker compose up -d --force-recreate controller
  ```

- **Verification:** confirm the new startup log line appears:

  ```bash
  docker compose logs controller --since 2m | grep "SNMP polling configuration"
  ```

  You should see something like:

  ```
  SNMP polling configuration: timeout=10.0s, retries=1 (discovery sweep uses 3s/1 internally)
  ```

  The parenthetical reminds you that the env vars govern polling
  only — the initial discovery sweep keeps its own tighter
  built-in timeout. See `CLAUDE.md` "Key Design Decisions Log"
  for the rationale.
