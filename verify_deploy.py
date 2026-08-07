"""Post-deploy check: does the live API serve the packs we just built?

A successful SFTP transfer proves bytes moved. It does not prove the served
API reads the directory they moved into, and the two came apart silently: the
pipeline deployed to jcodemunch.com for months while the shipped client
downloaded from a legacy host still serving an April build. Every user-visible
signal -- a 200, a valid zip, a plausible symbol count -- looked correct.

So the job now finishes by asking the API the same question a user's
`install-pack` asks, and compares the answer against dist/catalog.json.

Usage:  python verify_deploy.py [--api URL] [--timeout SECONDS]
Exit 0 = the live catalog matches what we built. Exit 1 = it does not.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The host the shipped client downloads from -- keep in step with
# jcodemunch_mcp/cli/install_pack.py:STARTER_PACK_API.
DEFAULT_API = "https://jcodemunch.com/starter-packs-system/api/index.php"

DIST = Path(__file__).resolve().parent / "dist"


# The CDN in front of jcodemunch.com answers a bare default header set with a
# 403 challenge, so every request from here carries the same shape the client
# sends (jcodemunch-mcp#417).
_HEADERS = {"User-Agent": "jcm-pack-verify", "X-JCM-Client": "jcm-pack-verify"}

def _fetch(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--catalog", default=None, help="built catalog to compare (default dist/catalog.json)")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=20.0)
    args = ap.parse_args()

    local_path = Path(args.catalog) if args.catalog else DIST / "catalog.json"
    if not local_path.exists():
        print(f"::error::no {local_path}; nothing was built, so nothing can be verified")
        return 1

    built = {
        p["id"]: p.get("version")
        for p in json.loads(local_path.read_text(encoding="utf-8")).get("packs", [])
    }
    if not built:
        print("::error::dist/catalog.json lists no packs")
        return 1

    # Retry: a just-written file can sit behind a cache for a beat. Retries
    # cover propagation lag only -- a wrong deploy target never resolves, so
    # the attempt count stays low enough that a real misconfiguration still
    # fails the job promptly.
    missing: list[str] = []
    stale: list[tuple[str, str, str]] = []
    for attempt in range(1, args.attempts + 1):
        try:
            live_doc = _fetch(f"{args.api}?action=catalog", args.timeout)
        except (urllib.error.URLError, ValueError) as exc:
            print(f"::error::could not read the live catalog at {args.api}: {exc}")
            return 1

        live = {p["id"]: p.get("version") for p in live_doc.get("packs", [])}
        missing = sorted(set(built) - set(live))
        stale = sorted(
            (pid, built[pid], live[pid]) for pid in set(built) & set(live)
            if built[pid] != live[pid]
        )
        if not missing and not stale:
            break
        if attempt < args.attempts:
            print(
                f"attempt {attempt}/{args.attempts}: "
                f"{len(missing) + len(stale)} pack(s) not served yet, retrying"
            )
            time.sleep(args.retry_delay)

    for pid in missing:
        print(f"::error::pack '{pid}' was built but the live catalog does not list it")
    for pid, want, got in stale:
        print(f"::error::pack '{pid}' serves v{got}; we built v{want}")

    if missing or stale:
        print(
            f"::error::the deploy reported success but {len(missing) + len(stale)} of "
            f"{len(built)} packs are not being served. Check that the deploy target is "
            f"the directory {args.api} reads from."
        )
        return 1

    # ⚠ The catalog agreeing is NOT the same as the DOWNLOAD being current.
    # On 2026-08-07 every check above passed while hcdn served the 2026-08-03
    # zip for a full day after the deploy -- the build whose indexes delete
    # themselves on the user's next server start (jcodemunch-mcp#419). The
    # catalog is generated per request; the zip is a cached object with its own
    # lifetime, and only one of those is what a user actually receives.
    #
    # So ask for the bytes and read the version off the response, the way
    # `install-pack` does. HEAD, not GET: the header is the whole answer and
    # the packs run to tens of megabytes.
    served_stale = []
    for pid, want in sorted(built.items()):
        url = f"{args.api}?action=download&pack={pid}"
        try:
            req = urllib.request.Request(url, method="HEAD", headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                got = resp.headers.get("X-Pack-Version")
                age = resp.headers.get("Age")
        except Exception as exc:
            # A licensed pack answers 403 without a key. That is the paywall
            # working, not a stale deploy, so it cannot fail this gate.
            print(f"  {pid:<15s} download not checkable ({exc.__class__.__name__})")
            continue
        if got and str(got) != str(want):
            served_stale.append((pid, want, got, age))

    for pid, want, got, age in served_stale:
        print(
            f"::error::pack '{pid}' DOWNLOAD serves v{got} but we built v{want}"
            + (f" (cache Age={age}s)" if age else "")
        )
    if served_stale:
        print(
            "::error::the catalog is current but the download is not. This is a CDN "
            "cache serving a previous build; purge it, and check that download URLs "
            "carry a per-build cache key."
        )
        return 1

    print(f"verified {len(built)} packs served at {args.api}")
    for pid in sorted(built):
        print(f"  {pid:<15s} v{built[pid]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
