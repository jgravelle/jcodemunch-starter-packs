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


def _fetch(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jcm-pack-verify"})
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

    print(f"verified {len(built)} packs served at {args.api}")
    for pid in sorted(built):
        print(f"  {pid:<15s} v{built[pid]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
