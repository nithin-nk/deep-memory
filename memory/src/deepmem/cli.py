"""`deepmem`: control what deep-memory records and keeps."""

import argparse
import re
import subprocess
import sys
import time

from pathlib import Path

from deepmem import distill, evaluate, store, vision

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
    forget = commands.add_parser("forget", help="permanently delete captured data and memories made from it")
    span = forget.add_mutually_exclusive_group(required=True)
    span.add_argument("--last", type=parse_duration, metavar="DURATION", help="delete the last 30m, 1h, 2d, ...")
    span.add_argument("--all", action="store_true", help="delete everything")
    forget.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    distill_cmd = commands.add_parser("distill", help="turn captured sessions into memories with the local model")
    distill_cmd.add_argument("--force", action="store_true", help="run even on battery while you are active")
    distill_cmd.add_argument("--watch", action="store_true", help="keep running, one pass every 5 minutes")
    export = commands.add_parser("export-fixture", help="freeze captured frames into a file for `deepmem eval`")
    export.add_argument("--last", type=parse_duration, metavar="DURATION", help="only the last 30m, 1h, 2d, ... (default: all)")
    export.add_argument("--out", type=Path, default=evaluate.FIXTURE, help=f"default: {evaluate.FIXTURE}")
    eval_cmd = commands.add_parser("eval", help="score distillation and recall on a fixture")
    eval_cmd.add_argument("--model", help="Ollama model to test (default: the configured one)")
    eval_cmd.add_argument("--fixture", type=Path, default=evaluate.FIXTURE, help=f"default: {evaluate.FIXTURE}")
    eval_cmd.add_argument("--cases", type=Path, default=evaluate.CASES, help=f"default: {evaluate.CASES}")
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
        print(f"screenshots:  {info['screenshots_waiting']} waiting for `deepmem distill`")
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
        print(f"Deleted {deleted} rows and memory files.")
    elif args.command == "distill":
        config = store.load_config()
        # ponytail: `--watch` is a sleep loop; launchd takes over in Slice 10.
        while True:
            if args.force or distill.allowed_now(config):
                try:
                    described = vision.describe_pending(config=config)
                    if described:
                        print(f"Described {described} screenshot(s).")
                    count = distill.run_once(config=config)
                    print(f"Wrote {count} episode(s) to {store.DB.parent / 'memories'}.")
                except distill.ModelUnavailable as err:
                    print(f"deepmem: {err}", file=sys.stderr)
                    if not args.watch:
                        return 1
            else:
                print("On battery and in use: skipped. Use --force to run anyway.")
            if not args.watch:
                break
            time.sleep(300)
    elif args.command == "export-fixture":
        count = evaluate.export_fixture(args.out, args.last)
        print(f"Wrote {count} frames to {args.out}. The file holds captured screen text: keep it private.")
        images = args.out.with_suffix(".images")
        if images.exists():
            print(f"Screenshots are in {images}: keep them private too.")
    elif args.command == "eval":
        for path in (args.fixture, args.cases):
            if not path.exists():
                print(f"deepmem: {path} not found (see README, Evaluation)", file=sys.stderr)
                return 1
        config = store.load_config()
        if args.model:
            config["model"] = args.model
        try:
            print(evaluate.format_report(evaluate.run(args.fixture, args.cases, config)))
        except distill.ModelUnavailable as err:
            print(f"deepmem: {err}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
