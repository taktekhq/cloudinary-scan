#!/usr/bin/env python3
"""Download a workflow run's encrypted shards, decrypt them and merge them into one file.

    python3 collect.py RUN_ID [RUN_ID ...] --repo OWNER/REPO --identity ~/.config/cloudinary-scan/age.key --out all.jsonl

Needs gh (logged in) and age. Shards are decrypted in memory, so no plaintext shard is written
to disk; only --out is, with mode 600. Rows are sorted by rank, one row per domain.
Use --dir to merge .age files you already downloaded instead of calling gh.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile


def merge(chunks):
    """Rows from JSON-lines strings, one per domain (first seen wins), sorted by rank."""
    seen = {}
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            seen.setdefault(row["domain"], row)
    return sorted(seen.values(), key=lambda r: r["rank"])


def decrypt(path, identity):
    out = subprocess.run(["age", "-d", "-i", identity, path], capture_output=True, check=True)
    return out.stdout.decode("utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_ids", nargs="*", metavar="RUN_ID", help="workflow run ids to download artifacts from")
    ap.add_argument("--repo", help="OWNER/REPO that ran the workflow (default: the current directory's repo)")
    ap.add_argument("--dir", help="folder holding downloaded .age files (skips the download)")
    ap.add_argument("--identity", required=True, help="age private key file")
    ap.add_argument("--out", required=True, help="merged JSON lines to write")
    a = ap.parse_args()
    if not a.run_ids and not a.dir:
        ap.error("give a run id or --dir")

    with tempfile.TemporaryDirectory() as tmp:
        src = a.dir
        if not src:
            for run_id in a.run_ids:
                cmd = ["gh", "run", "download", run_id, "-D", os.path.join(tmp, run_id), "-p", "scan-*"]
                if a.repo:
                    cmd += ["-R", a.repo]
                subprocess.run(cmd, check=True)
            src = tmp
        files = sorted(glob.glob(os.path.join(src, "**", "*.age"), recursive=True))
        if not files:
            sys.exit("no .age files found")
        rows = merge(decrypt(f, a.identity) for f in files)

    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(files)} shards, {len(rows)} sites -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
