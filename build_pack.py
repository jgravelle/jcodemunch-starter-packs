#!/usr/bin/env python
"""build_pack.py -- build Starter Pack archives from current jcm SQLite indexes.

Replaces the obsolete build-pack.sh, which targeted the pre-SQLite
~/.code-index/<owner>/<repo>/index.json layout. The current jcm store keeps one
self-contained SQLite file per repo at <index-dir>/<owner>-<repo>.db, so a pack
is just those .db files plus a manifest, zipped under a <pack-id>/ prefix that
jcm's `install-pack` strips on extract.

Flow per pack:
  - decide if a rebuild is needed (see change detection below)
  - if so: (re)index each repo, copy its <owner>-<repo>.db into staging,
    write a manifest, zip to dist/<pack-id>.zip, stamp a date version
  - emit dist/catalog.json (the data the serving PHP reads) and update state.json

Change detection: a pack rebuilds when --force, or when the jcm engine version
changed, or when any of its repos' upstream default-branch HEAD moved since the
last build (recorded in state.json). Unchanged packs keep their version, so the
console's update prompt only fires when there's genuinely something new.

Versions are date-stamped (YYYY.MM.DD): monotonic, and trivially drive the
console's `update_available` (installed marker version != catalog version).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKS_DEF = ROOT / "packs.json"
STATE_FILE = ROOT / "state.json"
DIST = ROOT / "dist"
MCP_BIN = os.environ.get("JMUNCH_MCP_BIN", "jcodemunch-mcp")


def _index_dir() -> Path:
    return Path(os.environ.get("CODE_INDEX_PATH") or (Path.home() / ".code-index"))


def _db_path(repo: str) -> Path:
    """The SQLite index file for a repo: owner/name -> <index-dir>/owner-name.db."""
    return _index_dir() / (repo.replace("/", "-") + ".db")


def _jcm_version() -> str:
    """The jcm engine version — a bump means the extractor may have improved, so
    every pack is worth rebuilding. Falls back to the CLI if metadata is absent."""
    try:
        import importlib.metadata as md
        return md.version("jcodemunch-mcp")
    except Exception:
        try:
            out = subprocess.run([MCP_BIN, "--version"], capture_output=True, text=True, timeout=15)
            return (out.stdout or out.stderr).strip() or "unknown"
        except Exception:
            return "unknown"


def _upstream_head(repo: str) -> str | None:
    """Current default-branch HEAD sha for a public GitHub repo (no token needed)."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True, text=True, timeout=45,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.split()[0]
    except Exception:
        pass
    return None


def _list_repos() -> dict:
    """jcm's own per-repo accounting (symbol_count etc.), keyed by repo_id."""
    try:
        out = subprocess.run([MCP_BIN, "list-repos", "--json"], capture_output=True, text=True, timeout=90)
        data = json.loads(out.stdout)
        return {r["repo_id"]: r for r in data if isinstance(r, dict) and r.get("repo_id")}
    except Exception:
        return {}


def _rmtree(path: Path) -> None:
    """Delete a tree, including git's read-only pack files on Windows.

    `shutil.rmtree(..., ignore_errors=True)` is not usable here: git marks
    objects read-only, rmtree raises PermissionError, and ignore_errors drops it
    on the floor leaving the directory in place -- so the next clone dies with
    "destination path already exists". Chmod and retry per entry instead.
    """
    if not path.exists():
        return

    def _retry(func, p, _excinfo):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_retry)          # Python 3.12+
    except TypeError:                              # 3.11 and earlier
        shutil.rmtree(path, onerror=_retry)


# Attribution files a permissive licence obliges us to carry with a copy. Matched
# at the CLONE ROOT only: a repo like django/django has hundreds of LICENSE files
# in test fixtures and vendored trees, and sweeping those in would bury the one
# that governs the code we ship.
_ATTRIBUTION_RE = re.compile(
    r"^(LICEN[SC]E|NOTICE|COPYING|COPYRIGHT|AUTHORS|PATENTS|THIRD[-_]PARTY)"
    r"[-_.A-Za-z0-9]*$",
    re.IGNORECASE,
)

LICENSE_CACHE = ROOT / ".license-cache"


def collect_attribution_files(clone: Path) -> list[tuple[str, bytes]]:
    """Attribution files at a clone's root, sorted, as (name, bytes).

    Pure over a directory so it is testable without cloning anything. Reads
    bytes rather than text: these files are copied verbatim, and decoding them
    would risk a re-encode changing the very bytes we are attesting to.
    """
    if not clone.is_dir():
        return []
    found = []
    for entry in sorted(clone.iterdir(), key=lambda p: p.name):
        if entry.is_file() and _ATTRIBUTION_RE.match(entry.name):
            found.append((entry.name, entry.read_bytes()))
    return found


def attribution_digest(files: list[tuple[str, bytes]]) -> str:
    """One sha256 over the whole attribution set: names and bytes.

    Covers a licence file being ADDED or REMOVED as well as edited, so a repo
    that quietly drops its NOTICE is as visible as one that rewrites its LICENSE.
    """
    h = hashlib.sha256()
    for name, data in files:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def _cache_attribution(repo: str, files: list[tuple[str, bytes]]) -> None:
    """Persist a repo's attribution files, since the clone is discarded at once."""
    dest = LICENSE_CACHE / repo.replace("/", "-")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (dest / name).write_bytes(data)


def _cached_attribution(repo: str) -> list[tuple[str, bytes]]:
    src = LICENSE_CACHE / repo.replace("/", "-")
    if not src.is_dir():
        return []
    return [(p.name, p.read_bytes()) for p in sorted(src.iterdir()) if p.is_file()]


def _run_index(repo: str) -> None:
    """Index a repo from a shallow clone rather than through the GitHub API.

    The API path costs roughly one request per tree and blob. On a runner the
    Actions-issued GITHUB_TOKEN is capped at 1,000 requests/hour per repository,
    and indexing nodejs/node alone exhausted it -- every later repo 403'd and the
    whole run built nothing while still reporting success.

    Cloning costs zero API quota. jcm resolves identity from
    `git remote get-url origin`, so a clone of owner/repo indexes as owner/repo
    and writes <index-dir>/owner-repo.db, the same file the API path produced.
    The clone is shallow and discarded as soon as the index is written, so peak
    disk is one repo, not ten.
    """
    # The clone path must be STABLE across runs, not a random temp dir. jcm keys
    # an index to the working tree that produced it and refuses a second tree
    # under the same identity ("would overwrite it"), so a fresh mktemp path
    # every run fails every pack as soon as the index dir isn't empty. A fixed
    # path per repo makes the re-index an update instead of a collision.
    clone = Path(tempfile.gettempdir()) / "jcm-pack-clones" / repo.replace("/", "-")
    _rmtree(clone)
    clone.parent.mkdir(parents=True, exist_ok=True)
    try:
        print(f"  cloning {repo} ...", flush=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet",
             f"https://github.com/{repo}", str(clone)],
            check=True,
        )
        # Capture attribution BEFORE indexing, so a failing index still leaves
        # the licence evidence behind for the next run to compare against.
        _cache_attribution(repo, collect_attribution_files(clone))
        print(f"  indexing {repo} ...", flush=True)
        subprocess.run([MCP_BIN, "index", str(clone)], check=True)
    finally:
        # Discard immediately so peak disk is one repo, not ten. Deleting is
        # safe: the next run recreates the identical path, which is what keeps
        # the identity check happy.
        _rmtree(clone)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def check_attribution(repo: str, declared: dict | None, prev_digest: str | None):
    """Gate a repo's attribution before its code can be packaged.

    Returns ``(files, digest, error)``. ``error`` is a string when the pack must
    NOT be rebuilt, in which case the previously built pack keeps shipping --
    the safe direction, since we already had the rights it was built under.

    Two failures block:

    * **No licence file at all.** We would be redistributing someone's work with
      nothing attached.
    * **The attribution bytes changed since the last build.** Usually harmless
      (a copyright year), but relicensing looks identical from here, and the one
      case we must never handle automatically is a repo moving to terms that
      forbid what the pack does. A human looks, then bumps the digest.
    """
    files = _cached_attribution(repo)
    if not files:
        return [], "", (
            f"no LICENSE/NOTICE/AUTHORS file found at the root of {repo}; "
            f"refusing to package it"
        )

    digest = attribution_digest(files)
    if declared is None:
        return files, digest, f"{repo} has no entry in packs.json repo_licenses"

    if prev_digest and prev_digest != digest:
        names = ", ".join(n for n, _ in files)
        return files, digest, (
            f"attribution for {repo} changed since the last build "
            f"({names}). Declared: {declared.get('spdx')}. Review the upstream "
            f"licence, then update repo_license_digests in state.json to "
            f"{digest[:16]}... to accept it"
        )
    return files, digest, None


def build(no_index: bool, force: bool) -> int:
    packs_doc = _load(PACKS_DEF, {})
    defs = packs_doc.get("packs", [])
    licenses = packs_doc.get("repo_licenses", {})
    if not defs:
        print("no packs defined in packs.json", file=sys.stderr)
        return 1
    state = _load(STATE_FILE, {})
    digests = state.setdefault("repo_license_digests", {})
    engine = _jcm_version()
    today = datetime.date.today().strftime("%Y.%m.%d")
    DIST.mkdir(exist_ok=True)

    catalog, changed, failed = [], [], []
    for pack in defs:
        pid = pack["id"]
        repos = pack["repos"]
        prev = state.get(pid, {})
        prev_cat = prev.get("catalog", {})

        # Upstream shas (skip the network probe in --no-index runs).
        cur_sha = (
            {r: _upstream_head(r) for r in repos}
            if not no_index else dict(prev.get("repos", {}))
        )
        moved = any(cur_sha.get(r) != prev.get("repos", {}).get(r) for r in repos)
        need = force or prev.get("engine") != engine or moved

        version = prev.get("version") or "1.0.0"
        filename = f"{pid}.zip"

        if need:
            print(f"== building {pid} ==")
            if not no_index:
                try:
                    for r in repos:
                        _run_index(r)
                except subprocess.CalledProcessError as e:
                    print(f"::warning::index failed for {pid}: {e} -- keeping prior pack")
                    failed.append(pid)
                    if prev_cat:
                        catalog.append(prev_cat)
                    continue

            dbs = {r: _db_path(r) for r in repos}
            missing = [r for r, p in dbs.items() if not p.exists()]
            if missing:
                print(f"::warning::missing index db for {missing} -- skipping {pid}")
                failed.append(pid)
                if prev_cat:
                    catalog.append(prev_cat)
                continue

            # Attribution gate. Runs after indexing (which is what populates the
            # licence cache) and before anything is packaged, so a repo we may
            # not redistribute never reaches a zip.
            attribution, blocked = {}, None
            for r in repos:
                files, digest, err = check_attribution(
                    r, licenses.get(r), digests.get(r)
                )
                if err:
                    blocked = err
                    break
                attribution[r] = (files, digest)
            if blocked:
                print(f"::error::{pid}: {blocked}")
                failed.append(pid)
                if prev_cat:
                    catalog.append(prev_cat)
                continue

            repo_meta = _list_repos()
            symbols = sum(int(repo_meta.get(r, {}).get("symbol_count", 0)) for r in repos)
            version = today

            with tempfile.TemporaryDirectory() as td:
                staging = Path(td) / pid
                staging.mkdir()
                for r, p in dbs.items():
                    shutil.copy2(p, staging / p.name)

                # Attribution travels inside the pack, one directory per repo,
                # byte-for-byte as upstream published it.
                licence_block = []
                for r in repos:
                    files, digest = attribution[r]
                    slug = r.replace("/", "-")
                    ldir = staging / "licenses" / slug
                    ldir.mkdir(parents=True, exist_ok=True)
                    for fname, data in files:
                        (ldir / fname).write_bytes(data)
                    licence_block.append({
                        "repo": r,
                        "spdx": (licenses.get(r) or {}).get("spdx", ""),
                        "note": (licenses.get(r) or {}).get("note", ""),
                        "files": [f"licenses/{slug}/{n}" for n, _ in files],
                        "digest": digest,
                        "commit": cur_sha.get(r) or "",
                    })
                    digests[r] = digest

                manifest = {
                    "pack_id": pid,
                    "name": pack["name"],
                    "version": version,
                    "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "repos": repos,
                    "total_symbols": symbols,
                    "format": "jcodemunch-sqlite",
                    "engine": engine,
                    "install_target": "~/.code-index/",
                    "licenses": licence_block,
                }
                (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

                archive = DIST / filename
                if archive.exists():
                    archive.unlink()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(staging.rglob("*")):
                        if f.is_file():
                            z.write(f, f"{pid}/{f.relative_to(staging)}")

            size_mb = round(archive.stat().st_size / 1_000_000, 2)
            state[pid] = {"version": version, "engine": engine, "repos": cur_sha}
            changed.append(pid)
            print(f"  built {filename}: {symbols:,} symbols, {size_mb} MB (v{version})")
        else:
            size_mb = prev_cat.get("size_mb", 0)
            symbols = prev_cat.get("symbols", 0)
            print(f"== {pid} up to date (v{version}) ==")

        entry = {
            "id": pid,
            "name": pack["name"],
            "description": pack.get("description", ""),
            "repos": repos,
            "free": bool(pack.get("free")),
            "version": version,
            "filename": filename,
            "symbols": symbols,
            "size_mb": size_mb,
            "size": f"{size_mb} MB",
            "indexed_date": today if need else prev_cat.get("indexed_date", today),
            # Surfaced in the catalog so `install-pack --list` can name the terms
            # BEFORE a download, not only after one.
            "licenses": (
                [{"repo": b["repo"], "spdx": b["spdx"]} for b in licence_block]
                if need else prev_cat.get("licenses", [])
            ),
        }
        catalog.append(entry)
        state.setdefault(pid, {})["catalog"] = entry

    (DIST / "catalog.json").write_text(
        json.dumps(
            {
                "packs": catalog,
                "free_count": sum(1 for c in catalog if c["free"]),
                "total_count": len(catalog),
                "generated": today,
                "engine": engine,
                "api_version": "2.0.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\nchanged packs: {', '.join(changed) if changed else 'none'}")
    if failed:
        print(f"failed packs: {', '.join(failed)}")
    print(f"catalog -> {DIST / 'catalog.json'}")

    # A run where every attempted pack failed built nothing and deployed nothing.
    # Exiting 0 there is what let a rate-limited run report success. Partial
    # failure stays a warning on purpose: one bad upstream must not discard the
    # packs that did build, since a non-zero exit here skips the deploy step.
    if failed and not changed:
        print(f"::error::no pack built; {len(failed)} failed")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Starter Pack archives from jcm SQLite indexes.")
    ap.add_argument("--no-index", action="store_true",
                    help="Skip re-indexing; package whatever .db files already exist (and skip the upstream-sha probe).")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild every pack regardless of change detection.")
    args = ap.parse_args()
    return build(no_index=args.no_index, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
