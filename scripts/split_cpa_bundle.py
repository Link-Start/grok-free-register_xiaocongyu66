#!/usr/bin/env python3
"""Split accounts.cpa.json (type=cpa-auth-bundle) into single-account xai-*.json.

CLIProxyAPI only accepts one auth credential per file with top-level type=xai.
The merged accounts.cpa.json has type=cpa-auth-bundle and is rejected / ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def accounts_from_bundle(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if data.get("type") == "xai":
            return [data]
        return list(data.get("accounts") or [])
    raise ValueError(f"unsupported bundle type: {type(data)!r}")


def account_filename(acc: dict) -> str:
    name = (acc.get("name") or "").strip()
    if name:
        stem = name[:-5] if name.endswith(".json") else name
        if not stem.startswith("xai-"):
            stem = f"xai-{stem}"
        return f"{stem}.json"
    key = (acc.get("email") or acc.get("sub") or "unknown").encode()
    return f"xai-{hashlib.sha256(key).hexdigest()[:16]}.json"


def normalize_account(acc: dict) -> dict | None:
    doc = {k: v for k, v in acc.items() if k != "name"}
    doc.setdefault("type", "xai")
    if doc.get("type") not in {"xai", "grok"}:
        if doc.get("auth_kind") == "oauth" or str(doc.get("base_url", "")).startswith(
            "https://api.x.ai"
        ):
            doc["type"] = "xai"
        else:
            return None
    doc["type"] = "xai"
    return doc


def split_bundle(src: Path, out_dir: Path) -> list[Path]:
    data = json.loads(src.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for acc in accounts_from_bundle(data):
        if not isinstance(acc, dict):
            continue
        doc = normalize_account(acc)
        if not doc:
            print(f"skip non-xai account: {acc.get('email') or acc.get('name')}", file=sys.stderr)
            continue
        path = out_dir / account_filename(acc if acc.get("name") else {**acc, "email": doc.get("email")})
        # recompute filename from doc if needed
        if not acc.get("name"):
            path = out_dir / account_filename({"email": doc.get("email"), "sub": doc.get("sub")})
        else:
            path = out_dir / account_filename(acc)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        written.append(path)
        print(f"wrote {path} type={doc.get('type')} email={doc.get('email')}")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "src",
        nargs="?",
        default="keys/cpa/accounts.cpa.json",
        help="path to accounts.cpa.json",
    )
    ap.add_argument(
        "-o",
        "--out-dir",
        default="keys/cpa",
        help="directory for xai-*.json (default: keys/cpa)",
    )
    ap.add_argument(
        "--import-cliproxy",
        default="",
        help="also copy singles into this CLIProxyAPI auth-dir (e.g. /root/CLIProxyAPI/auths)",
    )
    ap.add_argument(
        "--remove-bundle-from-import",
        action="store_true",
        help="delete accounts.cpa*.json from import dir so bulk is not loaded",
    )
    args = ap.parse_args()
    src = Path(args.src).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    if not src.is_file():
        print(f"missing {src}", file=sys.stderr)
        return 1
    written = split_bundle(src, out_dir)
    print(f"total={len(written)}")
    if args.import_cliproxy:
        dst = Path(args.import_cliproxy).expanduser()
        dst.mkdir(parents=True, exist_ok=True)
        if args.remove_bundle_from_import:
            for bad in dst.glob("accounts.cpa*.json"):
                bad.unlink()
                print(f"removed {bad}")
        for path in written:
            shutil.copy2(path, dst / path.name)
        print(f"imported {len(written)} files into {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
