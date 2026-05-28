from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DEFAULT_JSON_OUTPUT, DEFAULT_MASTER
from excel_tools import export_excel_to_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Export enriched company workbook to frontend JSON.")
    parser.add_argument("--input", default=str(DEFAULT_MASTER))
    parser.add_argument("--output", default=str(DEFAULT_JSON_OUTPUT))
    args = parser.parse_args()
    count = export_excel_to_json(Path(args.input), Path(args.output))
    print(f"Wrote {count} companies to {args.output}")


if __name__ == "__main__":
    main()
