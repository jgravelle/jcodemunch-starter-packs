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
| `packs.json` | Source of truth: pack id / name / description / free / repos, plus `repo_licenses`. Symbols, size, and version are computed at build time. |
| `build_pack.py` | The builder. Indexes changed repos, packages `.db` files and each repo's attribution, writes `dist/<pack-id>.zip` + `dist/catalog.json`, updates `state.json`. |
| `test_build_pack.py` | Tests for the attribution gate. CI runs them before anything is cloned. |
| `state.json` | Change-detection state (per-pack last-built upstream shas, jcm engine version, version) **and `repo_license_digests`**, the record of which attribution each pack was built against. Committed back by CI. |
| `.github/workflows/refresh-packs.yml` | Weekly + on-demand workflow: test, build, SFTP-deploy, verify the live catalog, commit state. |
| `dist/` | Build output (gitignored). Uploaded to Hostinger. |
| `.license-cache/` | Working copy of the attribution files pulled from each clone (gitignored; `state.json` holds the durable record). |

## Attribution

Every packed repo is third-party open source and nine of the ten packs are sold,
so each pack ships the upstream licence and attribution files **verbatim**, under
`licenses/<owner>-<name>/` inside the archive. `manifest.json` records, per repo,
the SPDX id, the files carried, a digest over them, and the commit the pack was
built from.

`repo_licenses` in `packs.json` is a **tripwire, not the source of truth**. The
builder copies whatever attribution files the clone actually has and refuses to
package a repo when either:

- **no licence file exists at its root** — we would be redistributing someone's
  work with nothing attached; or
- **the attribution bytes changed since the last build.** Usually that is a
  copyright year, but a relicensing looks identical from here, and the one case
  that must never be handled automatically is a repo moving to terms that forbid
  what the pack does. A blocked pack keeps shipping its previous build, which is
  the safe direction: we already had the rights that one was built under.

To accept a change, read the upstream diff, then update that repo's entry in
`state.json`'s `repo_license_digests` to the digest the error message prints.

Two repos are worth knowing about, both flagged in `packs.json`:

- **`nodejs/node`** — its `LICENSE` is a 157 KB compendium, MIT for Node itself
  followed by the licences of every bundled dependency (V8, ICU, OpenSSL and
  others). GitHub's own detector returns `NOASSERTION`. It ships whole; do not
  summarise it as MIT.
- **`modelcontextprotocol/typescript-sdk`** — mid-relicensing from MIT to
  Apache-2.0, with per-contribution status varying and documentation under
  CC-BY-4.0. Also `NOASSERTION`. The digest tripwire will fire when the
  transition completes.

Attribution files are matched at the **clone root only**. `django/django` carries
hundreds of `LICENSE` files in test fixtures and vendored trees, and sweeping
those in would bury the one that governs the code we ship.

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
