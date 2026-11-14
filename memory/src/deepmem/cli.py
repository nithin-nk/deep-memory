"""`deepmem`: control what deep-memory records and keeps."""

import argparse
import re
import subprocess
import sys

from deepmem import store

_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """'30m', '1h', '2d' -> seconds."""
    match = re.fullmatch(r"(\d+)([mhd])", value)
    if not match:
        raise argparse.ArgumentTypeError(f"expected a duration like 30m, 1h or 2d, got {value!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _daemon_running() -> bool:
    return subprocess.run(["pgrep", "-x", "deepmem-capture"], capture_output=True).returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deepmem", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pause", help="stop recording until `deepmem resume`")
    commands.add_parser("resume", help="start recording again")
    commands.add_parser("status", help="show capture state and how much is stored")
    forget = commands.add_parser("forget", help="permanently delete captured data")
    span = forget.add_mutually_exclusive_group(required=True)
    span.add_argument("--last", type=parse_duration, metavar="DURATION", help="delete the last 30m, 1h, 2d, ...")
    span.add_argument("--all", action="store_true", help="delete everything")
    forget.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    if args.command == "pause":
        store.HOME.mkdir(parents=True, exist_ok=True)
        store.PAUSED_FLAG.touch()
        print("Capture paused.")
    elif args.command == "resume":
        store.PAUSED_FLAG.unlink(missing_ok=True)
        print("Capture resumed.")
    elif args.command == "status":
        info = store.stats()
        if not _daemon_running():
            state = "not running"
        elif store.PAUSED_FLAG.exists():
            state = "paused"
        else:
            state = "recording"
        print(f"capture:      {state}")
        print(f"focus events: {info['events']}")
        print(f"text frames:  {info['frames']}")
        print(f"last capture: {info['last_capture'] or 'never'}")
        print(f"database:     {store.DB}")
    elif args.command == "forget":
        what = "ALL captured data" if args.all else "captured data from the selected period"
        if not args.yes:
            answer = input(f"Permanently delete {what}? This cannot be undone. [y/N] ")
            if answer.strip().lower() != "y":
                print("Nothing deleted.")
                return 1
        deleted = store.forget(None if args.all else args.last)
        print(f"Deleted {deleted} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
