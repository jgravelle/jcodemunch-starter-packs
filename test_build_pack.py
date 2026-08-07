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
    "anthropics/anthropic-sdk-python": ["LICENSE"],
    "anthropics/anthropic-sdk-typescript": ["LICENSE"],
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
