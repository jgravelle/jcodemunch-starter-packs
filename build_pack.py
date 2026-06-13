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
import json
import os
import shutil
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


def _run_index(repo: str) -> None:
    print(f"  indexing {repo} ...", flush=True)
    subprocess.run([MCP_BIN, "index", repo], check=True)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def build(no_index: bool, force: bool) -> int:
    defs = _load(PACKS_DEF, {}).get("packs", [])
    if not defs:
        print("no packs defined in packs.json", file=sys.stderr)
        return 1
    state = _load(STATE_FILE, {})
    engine = _jcm_version()
    today = datetime.date.today().strftime("%Y.%m.%d")
    DIST.mkdir(exist_ok=True)

    catalog, changed = [], []
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
                    print(f"  ! index failed for {pid}: {e} -- keeping prior pack")
                    if prev_cat:
                        catalog.append(prev_cat)
                    continue

            dbs = {r: _db_path(r) for r in repos}
            missing = [r for r, p in dbs.items() if not p.exists()]
            if missing:
                print(f"  ! missing index db for {missing} -- skipping {pid}")
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
    print(f"catalog -> {DIST / 'catalog.json'}")
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
