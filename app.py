#!/usr/bin/env python3
"""Desktop entry point and KiCad external BOM command."""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="Portable KiCad Library Manager")
    sub = parser.add_subparsers(dest="command")
    bom = sub.add_parser(
        "bom", help="Expand a KiCad XML BOM with connector accessories"
    )
    bom.add_argument("--library", required=True)
    bom.add_argument("--input", required=True)
    bom.add_argument("--output", required=True)
    parser.add_argument("--library", help="Explicit portable library directory")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == "bom":
        from manager.storage import Library
        from manager.bom import export

        print(
            json.dumps(export(Library(args.library), args.input, args.output), indent=2)
        )
    else:
        if args.library:
            from manager.conflicts import check

            check(args.library)
        from manager.server import run

        run(args.library, args.port, not args.no_browser, desktop=args.desktop, parent_pid=args.parent_pid)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
