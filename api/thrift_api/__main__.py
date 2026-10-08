"""`python -m thrift_api migrate`: creates the tables in DATABASE_URL's database (safe to run again). Run from api/."""
from __future__ import annotations

import os
import sys

from .db import Database
from .schema import migrate

USAGE = "usage: python -m thrift_api migrate   (reads DATABASE_URL)"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["migrate"]:
        print(USAGE, file=sys.stderr)
        return 2
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    db = Database(url)
    try:
        migrate(db)
    finally:
        db.close()
    print(f"migrated ({db.kind})")                  # never the URL: it carries the password
    return 0


if __name__ == "__main__":
    sys.exit(main())
