"""Tests for the attribution gate.

Every packed repo is third-party OSS and nine of ten packs are sold, so the pack
carries each upstream's licence and attribution files verbatim. These tests hold
two properties: that we cannot package a repo whose licence we did not capture,
and that a licence changing upstream stops the build rather than shipping
quietly under terms nobody re-read.

Run: python -m pytest test_build_pack.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import build_pack as bp

ROOT = Path(__file__).resolve().parent


# ── collect_attribution_files ─────────────────────────────────────────────

def test_collects_the_common_spellings(tmp_path):
    for name in ("LICENSE", "LICENSE.txt", "LICENCE", "LICENSE.md",
                 "NOTICE", "COPYING", "AUTHORS", "PATENTS",
                 "THIRD-PARTY-NOTICES", "LICENSE.python"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    got = [n for n, _ in bp.collect_attribution_files(tmp_path)]
    assert set(got) == {
        "LICENSE", "LICENSE.txt", "LICENCE", "LICENSE.md", "NOTICE", "COPYING",
        "AUTHORS", "PATENTS", "THIRD-PARTY-NOTICES", "LICENSE.python",
    }


def test_ignores_ordinary_files(tmp_path):
    (tmp_path / "LICENSE").write_text("mit", encoding="utf-8")
    for name in ("README.md", "licensing_test.py", "setup.py", "unlicense_me.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [n for n, _ in bp.collect_attribution_files(tmp_path)] == ["LICENSE"]


def test_root_only_never_recurses(tmp_path):
    """django/django carries hundreds of LICENSE files in fixtures and vendored
    trees. Sweeping those in would bury the one that governs the shipped code."""
    (tmp_path / "LICENSE").write_text("real", encoding="utf-8")
    nested = tmp_path / "tests" / "fixtures"
    nested.mkdir(parents=True)
    (nested / "LICENSE").write_text("a fixture, not our terms", encoding="utf-8")
    got = bp.collect_attribution_files(tmp_path)
    assert [n for n, _ in got] == ["LICENSE"]
    assert got[0][1] == b"real"


def test_missing_clone_is_empty_not_an_error(tmp_path):
    assert bp.collect_attribution_files(tmp_path / "nope") == []


# ── attribution_digest ────────────────────────────────────────────────────

def test_digest_is_stable_and_order_independent():
    a = bp.attribution_digest([("LICENSE", b"mit"), ("NOTICE", b"n")])
    b = bp.attribution_digest([("LICENSE", b"mit"), ("NOTICE", b"n")])
    assert a == b


def test_digest_moves_when_bytes_change():
    before = bp.attribution_digest([("LICENSE", b"Copyright 2025")])
    after = bp.attribution_digest([("LICENSE", b"Copyright 2026")])
    assert before != after


def test_digest_moves_when_a_file_is_removed():
    """A repo dropping its NOTICE must be as visible as one rewriting it."""
    both = bp.attribution_digest([("LICENSE", b"a"), ("NOTICE", b"b")])
    one = bp.attribution_digest([("LICENSE", b"a")])
    assert both != one


def test_digest_moves_when_only_a_filename_changes():
    assert (
        bp.attribution_digest([("LICENSE", b"same")])
        != bp.attribution_digest([("COPYING", b"same")])
    )


# ── check_attribution ─────────────────────────────────────────────────────

@pytest.fixture
def cached(tmp_path, monkeypatch):
    """Point the licence cache at a temp dir and return a writer for it."""
    monkeypatch.setattr(bp, "LICENSE_CACHE", tmp_path / "cache")

    def write(repo, files):
        bp._cache_attribution(repo, files)

    return write


def test_a_repo_with_no_licence_is_refused(cached):
    cached("acme/thing", [])
    _, _, err = bp.check_attribution("acme/thing", {"spdx": "MIT"}, None)
    assert err and "no LICENSE" in err


def test_an_undeclared_repo_is_refused(cached):
    """A repo missing from packs.json has had no licence review."""
    cached("acme/thing", [("LICENSE", b"mit")])
    _, _, err = bp.check_attribution("acme/thing", None, None)
    assert err and "repo_licenses" in err


def test_first_build_records_without_blocking(cached):
    cached("acme/thing", [("LICENSE", b"mit")])
    files, digest, err = bp.check_attribution("acme/thing", {"spdx": "MIT"}, None)
    assert err is None
    assert digest and [n for n, _ in files] == ["LICENSE"]


def test_unchanged_licence_passes(cached):
    cached("acme/thing", [("LICENSE", b"mit")])
    _, digest, _ = bp.check_attribution("acme/thing", {"spdx": "MIT"}, None)
    _, _, err = bp.check_attribution("acme/thing", {"spdx": "MIT"}, digest)
    assert err is None


def test_a_relicensing_upstream_blocks_the_build(cached):
    """The case this exists for: MIT to something that forbids redistribution
    looks exactly like a copyright-year bump from here, so both stop and a
    human reads the diff."""
    cached("acme/thing", [("LICENSE", b"MIT terms")])
    _, old, _ = bp.check_attribution("acme/thing", {"spdx": "MIT"}, None)

    cached("acme/thing", [("LICENSE", b"AGPL terms")])
    _, _, err = bp.check_attribution("acme/thing", {"spdx": "MIT"}, old)
    assert err and "changed since the last build" in err


# ── the real repos ────────────────────────────────────────────────────────

# Observed at each repo root 2026-07-31 via the GitHub contents API. Synthetic
# filenames prove the regex matches what we imagined; this proves it matches
# what upstream actually ships, which is where the spellings diverge
# (LICENSE.txt, LICENSE.md, LICENSE.python).
OBSERVED_ROOTS = {
    "nodejs/node": ["LICENSE"],
    "expressjs/express": ["LICENSE"],
    "fastify/fastify": ["LICENSE"],
    "koajs/koa": ["AUTHORS", "LICENSE"],
    "fastapi/fastapi": ["LICENSE"],
    "django/django": ["AUTHORS", "LICENSE", "LICENSE.python"],
    "pallets/flask": ["LICENSE.txt"],
    "facebook/react": ["LICENSE"],
    "langchain-ai/langchain": ["LICENSE"],
    "laravel/framework": ["LICENSE.md"],
    "spring-projects/spring-boot": ["LICENSE.txt"],
    "anthropics/anthropic-sdk-python": ["LICENSE", "LICENSE-MIT"],
    "anthropics/anthropic-sdk-typescript": ["LICENSE", "LICENSE-MIT"],
    "modelcontextprotocol/python-sdk": ["LICENSE"],
    "modelcontextprotocol/typescript-sdk": ["LICENSE"],
}


@pytest.mark.parametrize("repo,names", sorted(OBSERVED_ROOTS.items()))
def test_every_real_repos_licence_file_is_matched(repo, names, tmp_path):
    clone = tmp_path / repo.replace("/", "-")
    clone.mkdir()
    for n in names:
        (clone / n).write_text("terms", encoding="utf-8")
    # Plausible neighbours that must NOT be swept in.
    for n in ("README.md", "package.json", "setup.cfg"):
        (clone / n).write_text("x", encoding="utf-8")

    got = [n for n, _ in bp.collect_attribution_files(clone)]
    assert got == sorted(names), f"{repo}: captured {got}, expected {sorted(names)}"


def test_the_observed_set_covers_every_packed_repo():
    """If a repo joins a pack, its real licence filenames must be recorded here."""
    doc = json.loads((ROOT / "packs.json").read_text(encoding="utf-8"))
    used = {r for p in doc["packs"] for r in p["repos"]}
    assert used - set(OBSERVED_ROOTS) == set()


# ── packs.json ────────────────────────────────────────────────────────────

def test_every_packed_repo_has_a_declared_licence():
    doc = json.loads((ROOT / "packs.json").read_text(encoding="utf-8"))
    declared = set(doc["repo_licenses"])
    used = {r for p in doc["packs"] for r in p["repos"]}
    assert used - declared == set(), "a packed repo with no licence declaration"


def test_declared_licences_name_an_spdx_id():
    doc = json.loads((ROOT / "packs.json").read_text(encoding="utf-8"))
    for repo, meta in doc["repo_licenses"].items():
        assert meta.get("spdx"), f"{repo} has no spdx value"


# ── builder paths must never ship inside a pack (jcodemunch-mcp#419) ──────────
#
# We index from clones under `<tempdir>/jcm-pack-clones/<owner>-<repo>`, and that
# absolute path lands in each index's `meta`. Shipped as-is it names a directory
# that exists on this runner and nowhere else, and the client's startup orphan
# sweep deletes any index whose non-empty `source_root` is not a directory — so
# every installed pack was destroyed on the next server start (@MotoMato85).
#
# The client fixes this too, from 1.108.251. Neutralising here as well is what
# protects seats on OLDER clients: they receive a pack that was never poisoned.
# That is the whole reason this is not redundant, so do not delete it as such.

import sqlite3


def _mk_index(path, source_root):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [("repo", "expressjs/express"), ("source_root", source_root),
         ("git_root", source_root), ("indexed_at", "2026-08-06")],
    )
    conn.commit()
    conn.close()


def _meta(path, key):
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_neutralize_blanks_both_path_keys(tmp_path):
    db = tmp_path / "expressjs-express.db"
    _mk_index(db, "/tmp/jcm-pack-clones/expressjs-express")
    bp.neutralize_builder_paths(db)
    assert _meta(db, "source_root") == ""
    assert _meta(db, "git_root") == "", (
        "git_root left set keeps the pack visible to the client's watch-all"
    )


def test_neutralize_leaves_other_meta_intact(tmp_path):
    """Only the two path keys. Blanking `repo` or `indexed_at` would break the
    index in a way no client-side repair could undo."""
    db = tmp_path / "expressjs-express.db"
    _mk_index(db, "/tmp/jcm-pack-clones/expressjs-express")
    bp.neutralize_builder_paths(db)
    assert _meta(db, "repo") == "expressjs/express"
    assert _meta(db, "indexed_at") == "2026-08-06"


def test_neutralize_is_idempotent(tmp_path):
    """Re-running a build over an already-clean staging copy must be a no-op."""
    db = tmp_path / "expressjs-express.db"
    _mk_index(db, "")
    bp.neutralize_builder_paths(db)
    bp.neutralize_builder_paths(db)
    assert _meta(db, "source_root") == ""


def test_build_neutralizes_every_staged_db(tmp_path):
    """The wiring, not just the helper: whatever `build` stages must be cleaned.

    Guards the real regression risk — someone adds a second `shutil.copy2` into
    staging and forgets the neutralise call beside it.
    """
    import inspect
    src = inspect.getsource(bp.build)
    copies = src.count("shutil.copy2(")
    calls = src.count("neutralize_builder_paths(")
    assert calls >= copies, (
        f"{copies} staging copies but only {calls} neutralise call(s) in build(); "
        "a staged .db that keeps its builder path will delete itself on the "
        "user's next server start"
    )


def test_blocking_message_carries_the_full_digest(tmp_path, monkeypatch):
    """The gate message is the ONLY place the new digest is surfaced, and it
    told you to paste a 16-char prefix into a field of 64-char sha256 values.
    A prefix never matches, so obeying the instruction blocked the repo forever.
    """
    files = [("LICENSE", b"BSD-3-Clause text")]
    monkeypatch.setattr(bp, "_cached_attribution", lambda repo: files)
    digest = bp.attribution_digest(files)
    _f, got, err = bp.check_attribution(
        "django/django", {"spdx": "BSD-3-Clause"}, prev_digest="0" * 64
    )
    assert err and digest in err, (
        f"blocking message must contain the full {len(digest)}-char digest; got: {err}"
    )
    assert "..." not in err.split(digest)[-1][:8]


# ── coverage: a pack that hit a cap never ships ──────────────────────────
#
# Seven of ten packs shipped 2,000 files of a larger repo, because the CLI
# index used the 2,000-file in-call cap before jcodemunch-mcp 1.108.419. The
# index recorded the cut (`file_cap_status.truncated`) and nothing read it.

def _mk_covered(path, *, cap=..., coverage=..., files=3, symbols=7):
    """An index with the meta keys the coverage check reads.

    ``cap`` / ``coverage``: a dict to store, None to leave the key out (an
    index that cannot account for itself), or the default for a complete one.
    """
    if cap is ...:
        cap = {"truncated": False}
    if coverage is ...:
        coverage = {"walk": "full", "files_indexed": files, "skip_counts": {"too_large": 0}}
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE files (path TEXT)")
    conn.execute("CREATE TABLE symbols (id TEXT)")
    rows = [("repo", "x/y"), ("source_root", "/tmp/c"), ("git_root", ""), ("git_head", "a" * 40)]
    if cap is not None:
        rows.append(("file_cap_status", json.dumps(cap)))
    if coverage is not None:
        rows.append(("coverage", json.dumps(coverage)))
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", rows)
    conn.executemany("INSERT INTO files (path) VALUES (?)", [(f"f{i}",) for i in range(files)])
    conn.executemany("INSERT INTO symbols (id) VALUES (?)", [(f"s{i}",) for i in range(symbols)])
    conn.commit()
    conn.close()


def test_a_complete_index_is_publishable(tmp_path):
    db = tmp_path / "x-y.db"
    _mk_covered(db)
    cov = bp.pack_coverage(db)
    assert cov["files_indexed"] == 3 and cov["files_found"] == 3
    assert cov["cap_hit"] is False and cov["publishable"] is True
    assert bp.coverage_refusal(cov) == []


def test_an_index_cut_by_the_file_cap_is_refused(tmp_path):
    db = tmp_path / "x-y.db"
    _mk_covered(db, cap={"truncated": True, "files_discovered": 2591,
                         "files_indexed": 2000, "max_folder_files": 2000}, files=2000)
    cov = bp.pack_coverage(db)
    assert cov["cap_hit"] is True and cov["files_found"] == 2591
    reasons = bp.coverage_refusal(cov)
    assert reasons and "2000 of 2591" in reasons[0]


@pytest.mark.parametrize("key,stored", [
    ("cap", None), ("coverage", None),      # the key is absent
    ("cap", {}), ("coverage", {}),          # jcodemunch-mcp's own spelling of unknown: "{}"
    ("cap", {"truncated": "no"}),           # present, but not the boolean the check reads
    ("coverage", {"skip_counts": None}),
    ("cap", True), ("coverage", []),        # valid JSON that is not an object
])
def test_an_index_that_cannot_account_for_itself_is_refused(tmp_path, key, stored):
    # Unknown is not complete: an engine that wrote no usable record could have
    # cut the repo without saying so.
    db = tmp_path / "x-y.db"
    _mk_covered(db, **{key: stored})
    cov = bp.pack_coverage(db)
    assert cov["publishable"] is False
    assert bp.coverage_refusal(cov)


def test_an_unreadable_index_refuses_instead_of_raising(tmp_path):
    db = tmp_path / "x-y.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    cov = bp.pack_coverage(db)
    assert cov["publishable"] is False and "unreadable" in cov["error"]
    assert bp.coverage_refusal(cov) == [cov["error"]]


def test_size_limit_skips_are_reported_and_do_not_refuse(tmp_path):
    # J, 2026-11-06: files over the per-file size limit are disclosed in the
    # manifest; they do not stop a pack.
    db = tmp_path / "x-y.db"
    _mk_covered(db, coverage={"files_indexed": 3, "skip_counts": {"too_large": 122}})
    cov = bp.pack_coverage(db)
    assert cov["size_limit_skipped"] == 122
    assert bp.coverage_refusal(cov) == []


def test_reading_coverage_writes_nothing(tmp_path):
    # In WAL mode, as jcodemunch-mcp stores its indexes. An ordinary connection
    # creates -wal and -shm just by reading; an immutable one creates neither.
    db = tmp_path / "x-y.db"
    _mk_covered(db)
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
    conn.close()
    assert not Path(f"{db}-wal").exists() and not Path(f"{db}-shm").exists()
    before = db.stat().st_mtime_ns
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kw):
        opened.append((str(target), kw.get("uri")))
        return real_connect(target, *args, **kw)

    import unittest.mock
    with unittest.mock.patch.object(bp.sqlite3, "connect", spy):
        assert bp.pack_coverage(db)["publishable"] is True
    # SQLite deletes -wal/-shm when the last connection closes, so their absence
    # afterwards proves nothing on its own; the open mode is the property.
    assert opened and all(uri and "mode=ro" in target for target, uri in opened), opened
    assert db.stat().st_mtime_ns == before
    assert not Path(f"{db}-wal").exists() and not Path(f"{db}-shm").exists()


def test_an_index_with_an_open_wal_refuses_but_is_still_read(tmp_path):
    # Immutable cannot see a WAL's contents and reports the tables missing, so
    # the index is read through it. But the pack copies the .db alone, so the
    # pack refuses until the WAL is merged.
    db = tmp_path / "x-y.db"
    writer = sqlite3.connect(str(db))
    writer.execute("PRAGMA journal_mode=wal")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    writer.execute("CREATE TABLE files (path TEXT)")
    writer.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("file_cap_status", json.dumps({"truncated": False})),
        ("coverage", json.dumps({"skip_counts": {}})),
    ])
    writer.execute("INSERT INTO files VALUES ('a')")
    writer.commit()
    try:
        assert Path(f"{db}-wal").stat().st_size > 0
        cov = bp.pack_coverage(db)
        assert cov["files_indexed"] == 1 and cov["cap_hit"] is False  # read, not "no such table"
        assert cov["publishable"] is False and "-wal" in cov["error"]
        assert bp.coverage_refusal(cov) == [cov["error"]]
    finally:
        writer.close()


# ── the file limit for one large repo ────────────────────────────────────

def test_only_node_raises_the_file_limit():
    # J, 2026-11-06: 40,000 for nodejs/node only (36,060 indexable files); every
    # other repo fits under the CLI's own 30,000.
    doc = json.loads((ROOT / "packs.json").read_text(encoding="utf-8"))
    limits = {r: n for p in doc["packs"] for r, n in (p.get("max_files") or {}).items()}
    assert limits == {"nodejs/node": 50000}
    for p in doc["packs"]:
        assert set(p.get("max_files") or {}) <= set(p["repos"]), p["id"]


def test_the_limit_reaches_the_index_command_and_nothing_else(monkeypatch):
    monkeypatch.delenv("JCODEMUNCH_MAX_FOLDER_FILES", raising=False)
    assert "JCODEMUNCH_MAX_FOLDER_FILES" not in bp.index_env(None)
    env = bp.index_env(40000)
    assert env["JCODEMUNCH_MAX_FOLDER_FILES"] == "40000"
    import os
    assert "JCODEMUNCH_MAX_FOLDER_FILES" not in os.environ


def test_run_index_passes_the_limit_to_the_cli(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env")))
        if cmd[0] == "git":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    monkeypatch.setattr(bp, "LICENSE_CACHE", tmp_path / "cache")
    monkeypatch.setattr(bp.tempfile, "gettempdir", lambda: str(tmp_path))
    bp._run_index("nodejs/node", max_files=40000)
    index_calls = [(c, e) for c, e in calls if c[0] == bp.MCP_BIN]
    assert index_calls and index_calls[0][1]["JCODEMUNCH_MAX_FOLDER_FILES"] == "40000"


# ── the wiring: build() refuses a cut pack and records coverage ──────────

@pytest.fixture
def builder(tmp_path, monkeypatch):
    idx = tmp_path / "idx"
    idx.mkdir()
    monkeypatch.setenv("CODE_INDEX_PATH", str(idx))
    monkeypatch.setattr(bp, "DIST", tmp_path / "dist")
    monkeypatch.setattr(bp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bp, "_cached_attribution", lambda repo: [("LICENSE", b"MIT")])
    monkeypatch.setattr(bp, "_list_repos", lambda: {})
    defs = tmp_path / "packs.json"
    defs.write_text(json.dumps({
        "repo_licenses": {"a/one": {"spdx": "MIT"}, "b/two": {"spdx": "MIT"}},
        "packs": [
            {"id": "whole", "name": "Whole", "free": False, "repos": ["a/one"]},
            {"id": "cut", "name": "Cut", "free": False, "repos": ["b/two"]},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(bp, "PACKS_DEF", defs)
    _mk_covered(idx / "a-one.db", coverage={"files_indexed": 3, "skip_counts": {"too_large": 1}})
    _mk_covered(idx / "b-two.db", cap={"truncated": True, "files_discovered": 50,
                                       "files_indexed": 20}, files=20)
    return tmp_path


def test_build_ships_the_whole_pack_and_refuses_the_cut_one(builder):
    rc = bp.build(no_index=True, force=True)
    assert rc == 0  # one pack built; a partial failure keeps the deploy
    assert (builder / "dist" / "whole.zip").exists()
    assert not (builder / "dist" / "cut.zip").exists()
    import zipfile
    with zipfile.ZipFile(builder / "dist" / "whole.zip") as z:
        manifest = json.loads(z.read("whole/manifest.json"))
    cov = manifest["coverage"]["a/one"]
    assert cov["publishable"] is True and cov["cap_hit"] is False
    assert cov["files_indexed"] == 3 and cov["size_limit_skipped"] == 1
    assert manifest["total_files"] == 3
    catalog = json.loads((builder / "dist" / "catalog.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in catalog["packs"]] == ["whole"]  # nothing earlier to keep for "cut"


def test_a_build_where_every_pack_is_cut_fails(builder, monkeypatch):
    _mk = builder / "idx" / "a-one.db"
    _mk.unlink()
    _mk_covered(_mk, cap={"truncated": True, "files_discovered": 9, "files_indexed": 3})
    assert bp.build(no_index=True, force=True) == 1


def test_build_hands_the_packs_json_limit_to_the_index_call(builder, monkeypatch):
    # End to end: packs.json's max_files must reach _run_index, not only the
    # helper. Dropping it in build() left Node's 40,000 with no guard.
    defs = json.loads(bp.PACKS_DEF.read_text(encoding="utf-8"))
    defs["packs"][0]["max_files"] = {"a/one": 40000}
    bp.PACKS_DEF.write_text(json.dumps(defs), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(bp, "_upstream_head", lambda r: "f" * 40)
    monkeypatch.setattr(bp, "_run_index", lambda r, max_files=None: seen.__setitem__(r, max_files))
    bp.build(no_index=False, force=True)
    assert seen == {"a/one": 40000, "b/two": None}


def test_an_unreadable_index_refuses_its_pack_and_the_build_goes_on(builder):
    bad = builder / "idx" / "b-two.db"
    bad.unlink()
    conn = sqlite3.connect(str(bad))
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    assert bp.build(no_index=True, force=True) == 0
    assert (builder / "dist" / "whole.zip").exists()
    assert not (builder / "dist" / "cut.zip").exists()
    assert (builder / "dist" / "catalog.json").exists()
