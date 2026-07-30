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
| `HOSTINGER_SFTP_USER` | SSH username, from hPanel -> Advanced -> SSH Access |
| `HOSTINGER_SFTP_PASS` | That SSH account's password |
| `HOSTINGER_SFTP_PORT` | **`65002`** on Hostinger shared hosting |
| `HOSTINGER_PACKS_PATH` | Absolute path to the served `packs/` dir, e.g. `/home/<user>/domains/<domain>/public_html/starter-packs-system/packs` |

Three things that will waste a run each if you guess them:

- **Port 65002, not 22.** Port 22 times out. FTP accounts (port 21) are useless
  here — the deploy action speaks SFTP over SSH, so the credentials must come
  from the SSH Access panel, and SSH must be enabled for the account.
- **Do not use `~/public_html`.** On a multi-domain account it is a symlink to
  whichever domain was provisioned first, which may not be the one you want. Go
  through `domains/<domain>/public_html` explicitly. The hPanel file manager
  follows the same symlink, so its breadcrumb looks correct while pointing at
  the wrong site.
- **A wrong path with a leading `/` resolves from the filesystem root**, so the
  failure reads `mkdir: cannot create directory '/public_html': Permission
  denied` rather than anything about your account.

Settle the path with one command instead of guessing:

```bash
ssh -p 65002 <user>@<host> "ls -d ~/domains/*/public_html/starter-packs-system"
```

Indexing needs **no** GitHub API token. `build_pack.py` shallow-clones each repo
and indexes the working tree, which costs zero API quota — see the note in
`_run_index` for why the API path is unusable in CI.

## Bootstrap

Done 2026-07-30; kept as the recovery procedure.

1. Push this repo to `jgravelle/jcodemunch-starter-packs`.
2. Add the secrets above.
3. Deploy `starter-packs-system/api/index.php`.
4. Run the workflow once (`force=true`) to rebuild all packs and seed `state.json`.
5. Confirm `validate.php`-gated downloads still work and the console's
   Starter Packs rail shows the new date versions.

Verify at the served endpoint, not at the CI step — a green deploy proves files
moved, not that they landed where `index.php` reads them:

```bash
curl -s "https://jcodemunch.com/starter-packs-system/api/index.php?action=catalog"
```

The hardcoded fallback registry pins every pack at `1.0.0`, so a date version in
that response is proof `catalog.json` is being read.
