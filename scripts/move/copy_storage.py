"""Copies every file of the storage buckets from one Supabase project to another.

Runs on the server inside the API image (it has the supabase package); steps in
docs/move-supabase.md. Reads OLD_SUPABASE_URL, OLD_SERVICE_KEY, NEW_SUPABASE_URL,
NEW_SERVICE_KEY from the environment and never prints them. Same paths in the new project, so
the storage_path columns copied by copy_db.sh still point at the files. A file that already
exists in the new project is skipped, so the script can be run again after a failure.

    python copy_storage.py            copy
    python copy_storage.py --check    count only (nothing is written)
"""

import os
import sys

from supabase import create_client

BUCKETS = ("documents", "form-templates", "pipeline-files")
PAGE = 1000


def files(bucket, prefix=""):
    """Every file path under prefix (folders are walked; the API lists one level at a time)."""
    offset = 0
    while True:
        rows = bucket.list(prefix, {"limit": PAGE, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
        for row in rows:
            path = f"{prefix}/{row['name']}" if prefix else row["name"]
            if row.get("id") is None:  # a folder
                yield from files(bucket, path)
            else:
                yield path, (row.get("metadata") or {}).get("mimetype") or "application/octet-stream"
        if len(rows) < PAGE:
            return
        offset += PAGE


def main() -> int:
    check = "--check" in sys.argv
    env = {k: os.environ.get(k, "") for k in ("OLD_SUPABASE_URL", "OLD_SERVICE_KEY", "NEW_SUPABASE_URL", "NEW_SERVICE_KEY")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("missing:", ", ".join(missing))
        return 1
    if env["OLD_SUPABASE_URL"].rstrip("/") == env["NEW_SUPABASE_URL"].rstrip("/"):
        print("OLD_SUPABASE_URL and NEW_SUPABASE_URL are the same project")
        return 1
    old = create_client(env["OLD_SUPABASE_URL"], env["OLD_SERVICE_KEY"])
    new = create_client(env["NEW_SUPABASE_URL"], env["NEW_SERVICE_KEY"])
    failed = 0
    for name in BUCKETS:
        src, dst = old.storage.from_(name), new.storage.from_(name)
        have = {p for p, _ in files(dst)}
        copied = skipped = 0
        errors: list[str] = []
        todo = list(files(src))
        for path, mimetype in todo:
            if path in have:
                skipped += 1
                continue
            if check:
                continue
            try:
                dst.upload(path, src.download(path), {"content-type": mimetype, "upsert": "false"})
                copied += 1
            except Exception as exc:  # noqa: BLE001 - reported per file, the rest still copied
                errors.append(f"{path}: {type(exc).__name__}: {str(exc)[:200]}")
        print(f"{name}: old {len(todo)}, already in new {skipped}, copied {copied}, failed {len(errors)}")
        if check:
            print(f"  (check only: {len(todo) - skipped} would be copied)")
        for e in errors:
            print("  FAILED", e)
        failed += len(errors)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
