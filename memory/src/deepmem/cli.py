"""`deepmem`: control what deep-memory records and keeps."""

import argparse
import os
import re
import subprocess
import sys
import time

from pathlib import Path

from deepmem import consolidate, context, distill, evaluate, install, store, vision

_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """'30m', '1h', '2d' -> seconds."""
    match = re.fullmatch(r"(\d+)([mhd])", value)
    if not match:
        raise argparse.ArgumentTypeError(f"expected a duration like 30m, 1h or 2d, got {value!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _daemon_running() -> bool:
    return subprocess.run(["pgrep", "-x", "deepmem-capture"], capture_output=True).returncode == 0


def _consolidate(config: dict) -> None:
    changed, deleted = consolidate.run(config=config)
    print(f"Consolidated {changed} fact change(s); retention deleted {deleted} old rows and files.")


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
    distill_cmd.add_argument("--scheduled", action="store_true", help="one pass that also consolidates once a day (used by the launchd agent)")
    commands.add_parser("consolidate", help="merge episodes into facts.md and preferences.md, then delete old captures and episodes")
    context_cmd = commands.add_parser("context", help="print a digest of the current project's memories (run by the Claude Code hook)")
    context_cmd.add_argument("--dir", type=Path, help="project directory (default: the current one)")
    commands.add_parser("install", help="start deep-memory at login and add the Claude Code hook")
    uninstall = commands.add_parser("uninstall", help="remove what `deepmem install` set up")
    data = uninstall.add_mutually_exclusive_group()
    data.add_argument("--keep-data", action="store_true", help="keep recorded data and memories without asking")
    data.add_argument("--delete-data", action="store_true", help="delete recorded data and memories without asking")
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
        for warning in install.protection(store.HOME):
            print(f"warning:      {warning}")
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
                    if (args.watch or args.scheduled) and consolidate.due():  # once a day, under the same power/idle rule
                        _consolidate(config)
                except distill.ModelUnavailable as err:
                    print(f"deepmem: {err}", file=sys.stderr)
                    if not args.watch:
                        return 1
            else:
                print("On battery and in use: skipped. Use --force to run anyway.")
            if not args.watch:
                break
            time.sleep(300)
    elif args.command == "consolidate":
        try:
            _consolidate(store.load_config())
        except distill.ModelUnavailable as err:
            print(f"deepmem: {err}", file=sys.stderr)
            return 1
    elif args.command == "context":
        # Claude Code runs hooks in the project directory and names it in the environment.
        digest = context.summary(args.dir or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        if digest:
            print(digest)
    elif args.command in ("install", "uninstall"):
        try:
            if args.command == "install":
                package = Path(__file__).resolve().parents[2]  # memory/, when run from a checkout
                if not (package / "pyproject.toml").exists():
                    raise install.InstallError("run install from the repository: uv run --directory <repository>/memory deepmem install")
                install.install(package.parent / "capture" / "target" / "release" / "deepmem-capture", package)
            else:
                delete = args.delete_data
                if not delete and not args.keep_data:
                    answer = input(f"Also delete all recorded data and memories in {store.HOME}? This cannot be undone. [y/N] ")
                    delete = answer.strip().lower() == "y"
                install.uninstall(delete)
        except install.InstallError as err:
            print(f"deepmem: {err}", file=sys.stderr)
            return 1
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
