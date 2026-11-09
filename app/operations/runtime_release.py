"""Print a source-only, secret-free manifest for offline release comparison."""

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from app.core.runtime_release import source_manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="Source root containing app/ and main.py")
    options = parser.parse_args(argv)
    source = source_manifest(options.root)
    print(json.dumps({"schema": "runtime-release.v1", "source": source}, sort_keys=True))
    return 0 if source["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
