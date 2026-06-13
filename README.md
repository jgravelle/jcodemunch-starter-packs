# jcodemunch-starter-packs

Build pipeline for the jCodeMunch Starter Packs. Re-indexes the pack repos with
the current jcm and publishes the pre-built SQLite indexes as downloadable pack
zips, on a weekly schedule, only rebuilding what actually changed.

This replaces the obsolete `build-pack.sh` (which targeted the pre-SQLite
`~/.code-index/<owner>/<repo>/index.json` layout). The current jcm store keeps
one self-contained SQLite file per repo at `<index-dir>/<owner>-<repo>.db`, so a
pack is just those `.db` files plus a manifest, zipped under a `<pack-id>/`
prefix that jcm's `install-pack` strips on extract.

## Layout

| File | Role |
|------|------|
| `packs.json` | Source of truth: pack id / name / description / free / repos. Symbols, size, and version are computed at build time. |
| `build_pack.py` | The builder. Indexes changed repos, packages `.db` files, writes `dist/<pack-id>.zip` + `dist/catalog.json`, updates `state.json`. |
| `state.json` | Change-detection state (per-pack last-built upstream shas, jcm engine version, version). Committed back by CI. |
| `.github/workflows/refresh-packs.yml` | Weekly + on-demand workflow: build, SFTP-deploy, commit state. |
| `dist/` | Build output (gitignored). Uploaded to Hostinger. |

## Change detection + versioning

A pack rebuilds when **any of its repos' upstream default-branch HEAD moved**, or
when **jcm's engine version changed**, since the last build. Unchanged packs keep
their version and aren't re-downloaded by users.

Versions are date-stamped (`YYYY.MM.DD`). The console compares the installed
pack marker's version against the catalog version to light its **update**
prompt, so a rebuild automatically offers an update to everyone who has the pack.

## Local use

```bash
pip install jcodemunch-mcp

# Full build: probe upstream, re-index changed repos, package, write catalog.
python build_pack.py

# Force a full rebuild regardless of change detection.
python build_pack.py --force

# Package whatever .db files already exist, no re-indexing (fast; for testing
# or an immediate refresh from a box that's already indexed the repos).
python build_pack.py --no-index --force
```

Output lands in `dist/` (`*.zip` + `catalog.json`).

## Deploy (GitHub Actions -> Hostinger)

The workflow uploads `dist/*` to the Hostinger `packs/` directory that
`starter-packs-system/api/index.php` serves. `api/index.php` reads
`packs/catalog.json` (CI-generated) for the live catalog, falling back to its
hardcoded registry if the file isn't there.

**Required repo secrets** (Settings -> Secrets and variables -> Actions):

| Secret | Value |
|--------|-------|
| `HOSTINGER_SFTP_HOST` | Hostinger SFTP host |
| `HOSTINGER_SFTP_USER` | SFTP username |
| `HOSTINGER_SFTP_PASS` | SFTP password |
| `HOSTINGER_SFTP_PORT` | SFTP port (optional; defaults to 22) |
| `HOSTINGER_PACKS_PATH` | Absolute path to the served `packs/` dir, e.g. `/home/<user>/public_html/jCodeMunch/starter-packs-system/packs` |

The `GITHUB_TOKEN` the workflow already has is passed to jcm's indexer to raise
GitHub API limits while indexing.

## Bootstrap

1. Push this repo to `jgravelle/jcodemunch-starter-packs`.
2. Add the secrets above.
3. Run the workflow once (`force=true`) to rebuild all packs and seed `state.json`.
4. Confirm `validate.php`-gated downloads still work and the console's
   Starter Packs rail shows the new date versions.
