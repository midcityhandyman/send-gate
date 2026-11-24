"""Command line helper for the rule pack review date.

    python -m send_gate init             create the database, date a sample pack
    python -m send_gate init --restamp   record that you re-read the sample pack today

Uses the same environment variables as the service (SEND_GATE_DB,
SEND_GATE_RULE_PACK). It needs no tokens and sends nothing.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import rules
from .api import gate_from_env
from .gate import GateError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m send_gate", description="send-gate setup helper")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create the database and show the rule pack review state")
    init.add_argument(
        "--restamp",
        action="store_true",
        help="sample pack only: record today as the day a person re-read it",
    )
    init.add_argument("--actor", default=os.environ.get("USERNAME") or os.environ.get("USER") or "operator")
    args = parser.parse_args(argv)

    try:
        gate = gate_from_env()
    except rules.RulePackError as exc:
        print(f"rule pack problem: {exc}", file=sys.stderr)
        return 2
    try:
        if args.restamp:
            gate.restamp_sample_pack(args.actor)
        pack = gate.rule_pack()
        age = rules.check_rule_pack_age(pack, gate.now())
    except GateError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    finally:
        gate.close()

    source = "set by this database at first run" if pack.review_date_source == rules.FIRST_RUN else "from the file"
    print(f"database     {os.environ.get('SEND_GATE_DB', 'send_gate.db')}")
    print(f"rule pack    {pack.version} ({'sample' if pack.sample else 'not a sample'})")
    print(f"reviewed_at  {pack.reviewed_at.isoformat()} ({source})")
    print(f"limits       caution after {pack.max_age_days} days, stop after {pack.hard_max_age_days} days")
    print(f"status       {age.level}: {age.detail}")
    return 0 if age.level != rules.STOP else 1


if __name__ == "__main__":
    raise SystemExit(main())
