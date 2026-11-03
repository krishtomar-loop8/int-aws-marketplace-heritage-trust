#!/usr/bin/env python3
"""Run the connectivity harness against live sources and print the report.

Requires credentials populated in Secrets Manager and AWS access. Exit code is
non-zero if any source check fails.

    python scripts/connectivity_check.py
"""

from __future__ import annotations

import json
import sys

from backend.harness import run_checks


def main() -> int:
    report = run_checks()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
