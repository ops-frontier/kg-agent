from __future__ import annotations

import json
import sys
from pathlib import Path

from .source import analyze_file


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    path, root, language = sys.argv[1:]
    result = analyze_file(Path(path), Path(root), language)
    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())