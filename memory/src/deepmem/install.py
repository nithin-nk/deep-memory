"""`deepmem install` and `deepmem uninstall`.

Install makes deep-memory start by itself and survive a reboot:

- the capture daemon becomes `~/Applications/DeepMemory.app`, signed with a
  certificate made on this machine, and a launchd agent starts it at login;
- the `deepmem` command is installed with `uv tool install`, and a second
  launchd agent runs `deepmem distill --scheduled` every 5 minutes;
- a Claude Code SessionStart hook runs `deepmem context`.

Every function takes the home directory and the command runner as
arguments, so tests run against a temporary directory and run nothing.
"""

import json
import os
import plistlib
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from deepmem import store

BUNDLE_ID = "com.deepmem.capture"
AGENTS = {"capture": BUNDLE_ID, "distill": "com.deepmem.distill"}
IDENTITY = "deep-memory local signing"
HOOK = "context"  # the hook command is `<path>/deepmem context`


class InstallError(Exception):
    """Install cannot go on; the message says what the user must do."""


def run(*command: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(c) for c in command], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as err:
        return subprocess.CompletedProcess(command, 1, "", str(err))


# --- the app ----------------------------------------------------------------


def signing_keychain(data_home: Path, run=run) -> Path | None:
    """A keychain in `~/.deepmem/signing/` holding a self-signed code-signing
    certificate, made on first use. None if it cannot be made.

    macOS ties the Accessibility and Screen Recording grants to the signature.
    With this certificate the signature stays the same when the daemon is
    rebuilt; signed ad hoc, every rebuild would need the grants again."""
    # ponytail: the keychain's password lies next to it (mode 0600), so any
    # program running as this user can sign as deep-memory and inherit its
    # grants. Move the key to the login keychain (one prompt per install) if
    # that matters.
    directory = data_home / "signing"
    keychain, password = directory / "deepmem.keychain-db", directory / "password"
    if keychain.exists() and password.exists():
        ok = run("security", "unlock-keychain", "-p", password.read_text(), keychain).returncode == 0
        return keychain if ok else None
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    password.touch(mode=0o600)
    password.write_text(secrets.token_hex(16))
    secret = password.read_text()
    (directory / "cert.cnf").write_text(
        "[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
        f"[dn]\nCN = {IDENTITY}\n"
        "[ext]\nbasicConstraints = critical,CA:false\nkeyUsage = critical,digitalSignature\n"
        "extendedKeyUsage = critical,codeSigning\n"
    )
    key, cert, bundle = directory / "key.pem", directory / "cert.pem", directory / "identity.p12"
    steps = [
        ("/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
         "-keyout", key, "-out", cert, "-config", directory / "cert.cnf"),
        ("/usr/bin/openssl", "pkcs12", "-export", "-inkey", key, "-in", cert, "-out", bundle,
         "-passout", f"pass:{secret}", "-name", IDENTITY),
        # A keychain file of its own: the login keychain and its search list stay untouched.
        ("security", "create-keychain", "-p", secret, keychain),
        ("security", "unlock-keychain", "-p", secret, keychain),
        ("security", "import", bundle, "-k", keychain, "-P", secret, "-T", "/usr/bin/codesign"),
        ("security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", secret, keychain),
    ]
    ok = all(run(*step).returncode == 0 for step in steps)
    for leftover in (key, cert, bundle, directory / "cert.cnf"):  # the key now lives in the keychain only
        leftover.unlink(missing_ok=True)
    if not ok:
        keychain.unlink(missing_ok=True)
        password.unlink(missing_ok=True)
        return None
    return keychain


def build_app(binary: Path, user_home: Path, data_home: Path, run=run, log=print) -> Path:
    """Copy the daemon into `~/Applications/DeepMemory.app` and sign it.
    Returns the path of the executable inside the bundle."""
    app = user_home / "Applications" / "DeepMemory.app"
    shutil.rmtree(app, ignore_errors=True)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    executable = app / "Contents" / "MacOS" / "deepmem-capture"
    shutil.copy2(binary, executable)
    with (app / "Contents" / "Info.plist").open("wb") as file:
        plistlib.dump({
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": "DeepMemory",
            "CFBundleExecutable": "deepmem-capture",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.1.0",
            "LSUIElement": True,  # menu bar only, no Dock icon
        }, file)
    keychain = signing_keychain(data_home, run)
    if keychain and run("codesign", "--force", "--keychain", keychain, "-s", IDENTITY, app).returncode == 0:
        return executable
    if run("codesign", "--force", "-s", "-", app).returncode != 0:
        raise InstallError(f"cannot sign {app} (is codesign from the Xcode command line tools installed?)")
    log("Warning: signed without a certificate. After every update, grant Accessibility and "
        "Screen Recording to DeepMemory again.")
    return executable


# --- launchd ----------------------------------------------------------------


def _agent_path(user_home: Path, name: str) -> Path:
    return user_home / "Library" / "LaunchAgents" / f"{AGENTS[name]}.plist"


def write_agents(user_home: Path, data_home: Path, daemon: Path, deepmem: Path) -> None:
    logs = data_home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    common = {"EnvironmentVariables": {"DEEPMEM_HOME": str(data_home)}}
    agents = {
        "capture": {
            "ProgramArguments": [str(daemon)],
            "RunAtLoad": True,
            # Restart after a crash, but not after Quit in the menu bar.
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Interactive",
        },
        "distill": {
            # One pass: describe screenshots, distill closed sessions, and once
            # a day consolidate and apply retention. The command itself waits
            # for mains power or an idle user.
            "ProgramArguments": [str(deepmem), "distill", "--scheduled"],
            "StartInterval": 300,
            "ProcessType": "Background",
        },
    }
    for name, agent in agents.items():
        path = _agent_path(user_home, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        log = str(logs / f"{name}.log")
        with path.open("wb") as file:
            plistlib.dump({"Label": AGENTS[name], **agent, **common, "StandardOutPath": log, "StandardErrorPath": log}, file)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def stop_agents(run=run) -> None:
    for label in AGENTS.values():
        run("launchctl", "bootout", f"{_domain()}/{label}")  # fails when not loaded: fine


def start_agents(user_home: Path, run=run) -> None:
    for name in AGENTS:
        started = run("launchctl", "bootstrap", _domain(), _agent_path(user_home, name))
        if started.returncode != 0:
            raise InstallError(f"launchd refused {_agent_path(user_home, name)}: {started.stderr.strip()}")


# --- Claude Code hook -------------------------------------------------------


def _is_ours(entry) -> bool:
    hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
    return any(str(h.get("command", "")).endswith(f"deepmem {HOOK}") for h in hooks if isinstance(h, dict))


def set_hook(settings: Path, deepmem: Path | None) -> None:
    """Add (or with `deepmem=None` remove) the SessionStart hook in Claude
    Code's settings file. Every other key and hook stays as it is."""
    data = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise InstallError(f"{settings} is not valid JSON ({err}); fix it and run this again") from err
        if not isinstance(data, dict):
            raise InstallError(f"{settings} does not hold a JSON object")
    elif deepmem is None:
        return
    hooks = data.setdefault("hooks", {})
    entries = [entry for entry in hooks.get("SessionStart", []) if not _is_ours(entry)]
    if deepmem is not None:
        entries.append({"hooks": [{"type": "command", "command": f"{deepmem} {HOOK}"}]})
    if entries:
        hooks["SessionStart"] = entries
    else:
        hooks.pop("SessionStart", None)
        if not hooks:
            del data["hooks"]
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- checks -----------------------------------------------------------------


def protection(data_home: Path, run=run) -> list[str]:
    """Warnings about how the recorded data is protected at rest. deep-memory
    does not encrypt its files; it relies on the disk being encrypted and on
    the directory being closed to other users."""
    warnings = []
    if "FileVault is On" not in run("fdesetup", "status").stdout:
        warnings.append("FileVault is off: recorded data is not encrypted on disk. "
                        "Turn it on under System Settings > Privacy & Security > FileVault.")
    if data_home.exists() and data_home.stat().st_mode & 0o077:
        warnings.append(f"{data_home} can be read by other users of this Mac. Run: chmod 700 {data_home}")
    return warnings


def model_state(config: dict) -> str | None:
    """None if Ollama answers and has the configured model, else what is wrong."""
    try:
        with urllib.request.urlopen(config["ollama_url"].rstrip("/") + "/api/tags", timeout=3) as response:
            names = [model["name"] for model in json.load(response)["models"]]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return f"Ollama does not answer at {config['ollama_url']}. Start it; memories are made only while it runs."
    if config["model"] not in names:
        return f"The model is not there yet. Run: ollama pull {config['model']}"
    return None


# --- commands ---------------------------------------------------------------


def install(binary: Path, package: Path, user_home: Path | None = None, data_home: Path | None = None, run=run, log=print) -> None:
    """`binary`: the built daemon. `package`: the `memory/` directory."""
    user_home, data_home = user_home or Path.home(), data_home or store.HOME
    uv = shutil.which("uv")
    if not uv:
        raise InstallError("uv is not on the PATH (https://docs.astral.sh/uv/)")
    if not binary.exists():
        raise InstallError(f"{binary} is not built yet. Run: cd {binary.parents[2]} && cargo build --release")

    stop_agents(run)
    if run("pgrep", "-x", "deepmem-capture").returncode == 0:
        raise InstallError("a capture daemon started by hand is still running. Quit it (menu bar icon > Quit) and run this again.")

    data_home.mkdir(parents=True, exist_ok=True)
    data_home.chmod(0o700)
    daemon = build_app(binary, user_home, data_home, run, log)
    installed = run(uv, "tool", "install", "--force", "--reinstall", package)
    bin_dir = run(uv, "tool", "dir", "--bin").stdout.strip()
    if installed.returncode != 0 or not bin_dir:
        raise InstallError(f"uv tool install failed: {installed.stderr.strip()[-400:]}")
    deepmem = Path(bin_dir) / "deepmem"
    write_agents(user_home, data_home, daemon, deepmem)
    set_hook(user_home / ".claude" / "settings.json", deepmem)
    start_agents(user_home, run)

    log(f"Installed. The capture daemon runs now and at every login; memories are made every 5 minutes.\n"
        f"  App:      {daemon.parents[2]}\n"
        f"  Command:  {deepmem}\n"
        f"  Logs:     {data_home / 'logs'}\n"
        f"  Hook:     Claude Code sessions start with a digest of the project's memories.\n\n"
        "Still to do by hand:\n"
        "  1. System Settings > Privacy & Security > Accessibility: turn on DeepMemory.\n"
        "  2. System Settings > Privacy & Security > Screen & System Audio Recording: turn on DeepMemory.\n"
        "     Then restart it: menu bar icon > Quit, and open ~/Applications/DeepMemory.app.\n"
        f"  3. Register the search tools with Claude Code, once:\n"
        f"     claude mcp add --scope user deepmem -- {deepmem}-mcp")
    for warning in [*protection(data_home, run), model_state(store.load_config())]:
        if warning:
            log(f"Warning: {warning}")


def uninstall(delete_data: bool, user_home: Path | None = None, data_home: Path | None = None, run=run, log=print) -> None:
    user_home, data_home = user_home or Path.home(), data_home or store.HOME
    stop_agents(run)
    for name in AGENTS:
        _agent_path(user_home, name).unlink(missing_ok=True)
    shutil.rmtree(user_home / "Applications" / "DeepMemory.app", ignore_errors=True)
    set_hook(user_home / ".claude" / "settings.json", None)
    if delete_data:
        store.forget(None, db=data_home / "db.sqlite")  # overwrites what the database held
        run("security", "delete-keychain", data_home / "signing" / "deepmem.keychain-db")
        shutil.rmtree(data_home, ignore_errors=True)
        log(f"Deleted {data_home}.")
    else:
        log(f"Kept your data in {data_home}.")
    if uv := shutil.which("uv"):
        run(uv, "tool", "uninstall", "deepmem")  # last: this may be the running program
    log("Uninstalled. If you registered the search tools, also run: claude mcp remove deepmem")
