#!/usr/bin/env python3
"""Download the reference documents of documents/reference-docs.json into documents/ref/.

gen-docs.py writes documents of a known shape and size. The reference documents are real ones
that are hard on an office suite: a book of 960 pages of right-to-left text, a sheet of half a
million formulas, a deck of animated images. Most come from the attachments of open performance
bugs of LibreOffice, where users left the documents that were slow for them.

They are downloaded rather than committed: they are large, and the bug attachments carry no
license of their own. Each entry names its source and the SHA-256 of the file, so a document
that changed upstream fails the download rather than change what a test measures. A file that
is already there with the right checksum is not downloaded again.

    python3 documents/fetch-reference-docs.py            # every document
    python3 documents/fetch-reference-docs.py --list     # what there is, and what is missing
    python3 documents/fetch-reference-docs.py writer-rtl-book.docx
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "reference-docs.json"
OUT = HERE / "ref"
USER_AGENT = "cool-perf-dashboard/1 (reference documents)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str) -> bytes:
    """The body at @p url. The Bugzilla of LibreOffice answers 503 to a few downloads in a row,
    so a 503 is tried again after a pause."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for pause in (10, 30, 60, None):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 503 or pause is None:
                raise
            print(f"         503, again in {pause} s")
            time.sleep(pause)


def fetch(name: str, entry: dict) -> bool:
    dest = OUT / name
    if dest.exists() and sha256(dest.read_bytes()) == entry["sha256"]:
        print(f"ok       {name}")
        return True
    print(f"fetching {name} from {entry['url']}")
    try:
        data = download(entry["url"])
    except Exception as e:
        print(f"ERROR: {name}: {e}", file=sys.stderr)
        return False
    got = sha256(data)
    if got != entry["sha256"]:
        print(f"ERROR: {name}: sha256 {got}, expected {entry['sha256']}", file=sys.stderr)
        return False
    OUT.mkdir(exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    print(f"         {len(data) / 1e6:.1f} MB")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="documents to fetch (default: all)")
    ap.add_argument("--list", action="store_true", help="list the documents and stop")
    args = ap.parse_args()

    docs = json.loads(MANIFEST.read_text())
    unknown = [n for n in args.names if n not in docs]
    if unknown:
        print(f"not in {MANIFEST.name}: {', '.join(unknown)}", file=sys.stderr)
        return 2
    names = args.names or list(docs)
    if args.list:
        for n in names:
            have = "present" if (OUT / n).exists() else "missing"
            print(f"{n:32} {have:8} {docs[n]['about']}")
        return 0
    failed = [n for n in names if not fetch(n, docs[n])]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
