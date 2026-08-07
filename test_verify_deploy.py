"""The download gate must fail when the CDN serves a previous build.

On 2026-08-07 `verify_deploy.py` passed while hcdn served the 2026-08-03 zip
for a full day after a successful deploy -- the build whose indexes delete
themselves on the user's next server start (jcodemunch-mcp#419). Every existing
check agreed, because they all read the CATALOG, which is generated per request.
The zip is a cached object with its own lifetime, and it is the thing a user
actually receives.

⚠ The catalog gate returns early, so a live-API run cannot demonstrate the
download gate: making the catalog stale trips the earlier check first. These
tests stub the HTTP layer so the download gate is exercised in isolation.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_deploy as vd


class _Resp:
    def __init__(self, headers=None, body=None):
        self.headers = headers or {}
        self._body = body or b"{}"
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _wire(monkeypatch, *, catalog_version, download_version, age=None):
    """Catalog always agrees; only the download header varies."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "action=catalog" in url:
            return _Resp(body=json.dumps(
                {"packs": [{"id": "nodejs", "version": catalog_version}]}
            ).encode())
        h = {"X-Pack-Version": download_version}
        if age is not None:
            h["Age"] = str(age)
        return _Resp(headers=h)
    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)


def _catalog_file(tmp_path, version):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"packs": [{"id": "nodejs", "version": version}]}), encoding="utf-8")
    return p


def _run(tmp_path, monkeypatch, built, served, age=None):
    _wire(monkeypatch, catalog_version=built, download_version=served, age=age)
    monkeypatch.setattr(sys, "argv", [
        "verify_deploy.py", "--catalog", str(_catalog_file(tmp_path, built)), "--attempts", "1",
    ])
    return vd.main()


def test_stale_download_fails_even_when_the_catalog_agrees(tmp_path, monkeypatch, capsys):
    """The exact 2026-08-07 shape: catalog current, zip four days old."""
    rc = _run(tmp_path, monkeypatch, built="2026.08.07", served="2026.08.03", age=3166)
    out = capsys.readouterr().out
    assert rc == 1, "a stale DOWNLOAD passed verification"
    assert "DOWNLOAD serves v2026.08.03" in out
    assert "Age=3166s" in out, "cache age is the diagnostic; it must be surfaced"


def test_current_download_passes(tmp_path, monkeypatch):
    """CONTROL: green when catalog and download agree."""
    assert _run(tmp_path, monkeypatch, built="2026.08.07", served="2026.08.07") == 0


def test_licensed_pack_403_does_not_fail_the_gate(tmp_path, monkeypatch, capsys):
    """CONTROL: premium packs answer 403 without a key. That is the paywall
    working, and it must never be reported as a stale deploy."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "action=catalog" in url:
            return _Resp(body=json.dumps(
                {"packs": [{"id": "nodejs", "version": "2026.08.07"}]}).encode())
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", [
        "verify_deploy.py", "--catalog", str(_catalog_file(tmp_path, "2026.08.07")), "--attempts", "1",
    ])
    assert vd.main() == 0
    assert "not checkable" in capsys.readouterr().out


def test_missing_version_header_is_not_treated_as_stale(tmp_path, monkeypatch):
    """CONTROL: absence of X-Pack-Version is unknown, not wrong. Failing on it
    would break the gate against any future server that stops sending it."""
    assert _run(tmp_path, monkeypatch, built="2026.08.07", served=None) == 0
