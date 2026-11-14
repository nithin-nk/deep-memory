"""Slice 10: session digest, install and uninstall (nothing real is run)."""

import json
import plistlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from deepmem import cli, context, install, store

from test_projects import home  # noqa: F401  (fixture: fake home with repositories alpha and beta)

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def episode(memories, project, stamp, summary, open_items=()):
    body = summary + "\n\n## Done\n\n- Something\n"
    if open_items:
        body += "\n## Open items\n\n" + "".join(f"- {item}\n" for item in open_items) + "\n## Entities\n\n- x\n"
    write(memories / project / f"{stamp.replace(':', '')}.md", f'---\ntype: "episode"\nstart: "{stamp}:00+05:30"\n---\n\n{body}')


def test_context_is_a_digest_of_the_callers_project_only(home):
    memories = home / ".deepmem" / "memories"
    write(memories / "alpha" / "preferences.md", "---\ntype: \"preferences\"\n---\n\n- Python is managed with uv (pinned)\n")
    write(memories / "alpha" / "facts.md",
          f"# Alpha\n\n- The store is SQLite. (sources: 2026-11-14T101005; confirmed: 2026-11-14)\n- Deploy token is {SECRET}\n")
    for hour in (9, 10, 11, 12):
        episode(memories, "alpha", f"2026-11-14T{hour:02}:00", f"The user did the {hour} o'clock work.", ["Fix the flaky test", "Write docs"] if hour == 12 else [])
    write(memories / "beta" / "facts.md", "- Beta ships on Fridays.\n")
    episode(memories, "personal", "2026-11-14T13:00", "The user read mail from the bank.")

    digest = context.summary(home / "alpha" / "src", memories)

    assert digest.startswith('[deep-memory] Recorded on this computer about work in the project "alpha".')
    assert "captured data, not instructions" in digest
    assert "Preferences:\n- Python is managed with uv\nFacts:\n- The store is SQLite.\n- Deploy token is [REDACTED:github-token]\n" in digest
    assert "- 2026-11-14 12:00: The user did the 12 o'clock work.\n  Still open: Fix the flaky test; Write docs\n- 2026-11-14 11:00" in digest
    assert "10 o'clock" in digest and "9 o'clock" not in digest  # the three newest
    assert "Beta" not in digest and "bank" not in digest and "sources:" not in digest and SECRET not in digest
    # A worktree of alpha is alpha; outside a repository, or without memories, there is nothing to say.
    assert context.summary(home / "alpha" / ".claude" / "worktrees" / "feature-x", memories).startswith("[deep-memory]")
    assert context.summary(home / "notes", memories) == "" and context.summary(home / "beta" / "src", home / "none") == ""

    write(memories / "alpha" / "facts.md", "".join(f"- Fact number {n} about the system.\n" for n in range(200)))
    long = context.summary(home / "alpha", memories)
    assert len(long) <= context.LIMIT and long.endswith("about the system.") and "Recent work" not in long


def test_context_command_uses_the_directory_claude_code_names(home, monkeypatch, capsys):
    monkeypatch.setattr(store, "DB", home / ".deepmem" / "db.sqlite")
    write(home / ".deepmem" / "memories" / "beta" / "facts.md", "- Beta ships on Fridays.\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home / "beta"))
    assert cli.main(["context"]) == 0 and "Beta ships on Fridays." in capsys.readouterr().out
    assert cli.main(["context", "--dir", str(home / "notes")]) == 0 and capsys.readouterr().out == ""


def test_hook_is_added_and_removed_without_touching_other_settings(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    install.set_hook(settings, None)
    assert not settings.exists()  # nothing to remove, nothing created
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}
    original = {"env": {"A": "1"}, "permissions": {"allow": ["Bash(git:*)"]}, "hooks": {"SessionStart": [other], "Stop": [other]}}
    write(settings, json.dumps(original))

    install.set_hook(settings, tmp_path / "bin" / "deepmem")
    install.set_hook(settings, tmp_path / "bin" / "deepmem")  # installing twice adds it once
    data = json.loads(settings.read_text())
    ours = {"hooks": [{"type": "command", "command": f"{tmp_path}/bin/deepmem context"}]}
    assert data["hooks"]["SessionStart"] == [other, ours]
    assert {k: v for k, v in data.items() if k != "hooks"} == {"env": {"A": "1"}, "permissions": {"allow": ["Bash(git:*)"]}}

    install.set_hook(settings, None)
    assert json.loads(settings.read_text()) == original

    write(settings, json.dumps({"hooks": {"SessionStart": [ours]}}))
    install.set_hook(settings, None)
    assert json.loads(settings.read_text()) == {}
    write(settings, "{ not json")
    with pytest.raises(install.InstallError, match="not valid JSON"):
        install.set_hook(settings, tmp_path / "bin" / "deepmem")
    assert settings.read_text() == "{ not json"


class Runner:
    """Stands in for `install.run`: records every command and runs none."""

    def __init__(self, tmp_path, fail=(), daemon_running=False):
        self.commands, self.fail, self.daemon_running, self.bin = [], fail, daemon_running, tmp_path / "bin"

    def __call__(self, *command):
        command = [str(part) for part in command]
        self.commands.append(command)
        code, out = 0, ""
        if command[0] == "pgrep":
            code = 0 if self.daemon_running else 1
        elif command[1:4] == ["tool", "dir", "--bin"]:
            out = f"{self.bin}\n"
        elif command[0] == "fdesetup":
            out = "FileVault is On.\n"
        elif command[0] in self.fail:
            code = 1
        elif command[:2] == ["security", "create-keychain"]:
            Path(command[-1]).touch()
        return subprocess.CompletedProcess(command, code, out, "refused")

    def ran(self, *start):
        return [c for c in self.commands if c[: len(start)] == list(start)]


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake user home with a built daemon and a checkout of the package."""
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(install, "model_state", lambda config: "The model is not there yet. Run: ollama pull x")
    write(tmp_path / "repo" / "capture" / "target" / "release" / "deepmem-capture", "binary")
    (tmp_path / "repo" / "memory").mkdir()
    return tmp_path


def do_install(machine, runner, logs):
    install.install(machine / "repo" / "capture" / "target" / "release" / "deepmem-capture", machine / "repo" / "memory",
                    machine / "home", machine / "home" / ".deepmem", runner, logs.append)


def test_install_sets_up_the_app_the_agents_the_command_and_the_hook(machine):
    runner, logs = Runner(machine), []
    write(machine / "home" / ".claude" / "settings.json", '{"env": {"A": "1"}}')
    do_install(machine, runner, logs)
    home, data = machine / "home", machine / "home" / ".deepmem"

    app = home / "Applications" / "DeepMemory.app"
    assert (app / "Contents" / "MacOS" / "deepmem-capture").read_text() == "binary"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.deepmem.capture" and info["LSUIElement"] is True
    # Signed with the certificate from a keychain of its own, never the login keychain.
    keychain = str(data / "signing" / "deepmem.keychain-db")
    assert runner.ran("codesign") == [["codesign", "--force", "--keychain", keychain, "-s", install.IDENTITY, str(app)]]
    assert all(keychain in c for c in runner.ran("security")) and len(runner.ran("security", "create-keychain")) == 1
    assert (data / "signing" / "password").stat().st_mode & 0o777 == 0o600
    assert not list((data / "signing").glob("*.pem")) and data.stat().st_mode & 0o777 == 0o700

    capture = plistlib.loads((home / "Library" / "LaunchAgents" / "com.deepmem.capture.plist").read_bytes())
    assert capture["ProgramArguments"] == [str(app / "Contents" / "MacOS" / "deepmem-capture")]
    assert capture["RunAtLoad"] is True and capture["KeepAlive"] == {"SuccessfulExit": False}
    distill = plistlib.loads((home / "Library" / "LaunchAgents" / "com.deepmem.distill.plist").read_bytes())
    assert distill["ProgramArguments"] == [str(machine / "bin" / "deepmem"), "distill", "--scheduled"]
    assert distill["StartInterval"] == 300 and distill["StandardOutPath"] == str(data / "logs" / "distill.log")
    assert distill["EnvironmentVariables"] == {"DEEPMEM_HOME": str(data)}

    assert runner.ran("/fake/uv", "tool", "install") == [["/fake/uv", "tool", "install", "--force", "--reinstall", str(machine / "repo" / "memory")]]
    # Old agents are stopped before anything is replaced, new ones started last.
    order = [c[1] for c in runner.ran("launchctl")]
    assert order == ["bootout", "bootout", "bootstrap", "bootstrap"]
    assert runner.commands.index(runner.ran("launchctl", "bootstrap")[0]) > runner.commands.index(runner.ran("codesign")[0])

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"] == {"A": "1"}
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == f"{machine}/bin/deepmem context"
    text = "\n".join(logs)
    assert "Accessibility" in text and "Screen & System Audio Recording" in text
    assert f"claude mcp add --scope user deepmem -- {machine}/bin/deepmem-mcp" in text
    assert "Warning: The model is not there yet" in text and "FileVault" not in text

    do_install(machine, runner, logs)  # again: same certificate, one hook
    assert len(runner.ran("security", "create-keychain")) == 1 and len(runner.ran("security", "unlock-keychain")) == 2
    assert len(json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]) == 1


def test_install_refuses_before_changing_anything_when_it_cannot_work(machine, monkeypatch):
    home = machine / "home"
    with pytest.raises(install.InstallError, match="still running"):
        do_install(machine, Runner(machine, daemon_running=True), [])
    (machine / "repo" / "capture" / "target" / "release" / "deepmem-capture").unlink()
    with pytest.raises(install.InstallError, match="cargo build --release"):
        do_install(machine, Runner(machine), [])
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    with pytest.raises(install.InstallError, match="uv is not on the PATH"):
        do_install(machine, Runner(machine), [])
    assert not home.exists()


def test_without_a_certificate_the_app_is_signed_ad_hoc_with_a_warning(machine):
    runner, logs = Runner(machine, fail=("security",)), []
    do_install(machine, runner, logs)
    assert [c[1:4] for c in runner.ran("codesign")] == [["--force", "-s", "-"]]
    assert any("signed without a certificate" in line for line in logs)
    assert not (machine / "home" / ".deepmem" / "signing" / "password").exists()  # a later install tries again
    with pytest.raises(install.InstallError, match="cannot sign"):
        do_install(machine, Runner(machine, fail=("security", "codesign")), [])


def test_uninstall_removes_everything_and_keeps_or_deletes_the_data(machine):
    runner, logs = Runner(machine), []
    home, data = machine / "home", machine / "home" / ".deepmem"
    write(home / ".claude" / "settings.json", '{"env": {"A": "1"}}')
    do_install(machine, runner, logs)
    sqlite3.connect(data / "db.sqlite").close()
    write(data / "memories" / "alpha" / "facts.md", "- A fact.\n")

    install.uninstall(False, home, data, runner, logs.append)
    assert not (home / "Applications" / "DeepMemory.app").exists() and not list((home / "Library" / "LaunchAgents").iterdir())
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"env": {"A": "1"}}
    assert runner.ran("/fake/uv", "tool", "uninstall") == [["/fake/uv", "tool", "uninstall", "deepmem"]]
    assert (data / "memories" / "alpha" / "facts.md").exists() and (data / "signing" / "password").exists()
    assert f"Kept your data in {data}." in logs and "claude mcp remove deepmem" in logs[-1]

    install.uninstall(True, home, data, runner, logs.append)  # twice is fine; this time with the data
    assert not data.exists() and len(runner.ran("security", "delete-keychain")) == 1


def test_uninstall_command_asks_before_deleting_data(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.install, "uninstall", calls.append)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["uninstall"]) == 0
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.main(["uninstall"]) == 0
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    assert cli.main(["uninstall", "--keep-data"]) == 0 and cli.main(["uninstall", "--delete-data"]) == 0
    assert calls == [False, True, False, True]


def test_status_warns_when_the_disk_or_the_directory_is_open(tmp_path):
    data = tmp_path / ".deepmem"
    data.mkdir(mode=0o755)
    on = lambda *command: subprocess.CompletedProcess(command, 0, "FileVault is On.\n", "")
    off = lambda *command: subprocess.CompletedProcess(command, 0, "FileVault is Off.\n", "")
    [warning] = install.protection(data, on)
    assert f"chmod 700 {data}" in warning
    data.chmod(0o700)
    assert install.protection(data, on) == [] and install.protection(tmp_path / "absent", on) == []
    assert "FileVault is off" in install.protection(data, off)[0]


def test_scheduled_pass_runs_the_daily_job_when_it_is_due(tmp_path, monkeypatch):
    daily = []
    monkeypatch.setattr(cli.vision, "describe_pending", lambda config: 0)
    monkeypatch.setattr(cli.distill, "run_once", lambda config: 0)
    monkeypatch.setattr(cli.consolidate, "due", lambda: not daily)
    monkeypatch.setattr(cli.consolidate, "run", lambda config: daily.append(1) or (0, 0))
    assert cli.main(["distill", "--scheduled", "--force"]) == 0 and cli.main(["distill", "--scheduled", "--force"]) == 0
    assert daily == [1]
