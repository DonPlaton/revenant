"""OpenCode must receive the terminal directly, without a PowerShell npm shim."""

from pathlib import Path

import pytest

import revenant
import revenant_agents as agents
import revenant_terminals as terminals


def test_npm_native_binary_is_used_without_powershell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = tmp_path / "npm" / "opencode.cmd"
    binary = shim.parent / "node_modules/opencode-ai/bin/opencode.exe"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(agents.shutil, "which", lambda name: str(shim) if name == "opencode.cmd" else None)
    monkeypatch.setattr(terminals, "WINDOWS", True)
    session = revenant.Session("ses_abc", tmp_path / "opencode.db", "project",
                               agent=agents.AGENTS["opencode"], cwd=tmp_path, title="Repair parser")
    job = session.job()
    assert job.argv == (str(binary), "--session", "ses_abc")
    assert job.label == "Repair parser"
    for layout in terminals.LAYOUTS:
        plan = terminals.WindowsTerminal().plan([job], layout=layout)
        assert plan.commands[0][-3:] == list(job.argv)
        assert "-Command" not in plan.commands[0]
    console = terminals.WindowsConsole().plan([job])
    assert console.commands == [list(job.argv)]
    assert console.new_console and console.directory(0) == str(tmp_path)


def test_native_install_on_path_has_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents.shutil, "which", lambda name: r"D:\tools\opencode.exe" if name == "opencode.exe" else None)
    assert agents.AGENTS["opencode"].windows_argv("ses_abc") == (r"D:\tools\opencode.exe", "--session", "ses_abc")


def test_cmd_only_install_avoids_powershell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shim = tmp_path / "tools & apps" / "opencode.cmd"
    monkeypatch.setattr(agents.shutil, "which", lambda name: str(shim) if name == "opencode.cmd" else None)
    argv = agents.AGENTS["opencode"].windows_argv("ses_abc")
    assert argv == ("cmd.exe", "/d", "/k", f'""{shim}" --session ses_abc"')


@pytest.mark.parametrize("session_id", ["ses_abc;whoami", "ses_abc & whoami", "not-a-session"])
def test_native_launch_rejects_shell_injection(session_id: str) -> None:
    with pytest.raises(ValueError):
        agents.AGENTS["opencode"].windows_argv(session_id)


def test_missing_native_install_retains_resume_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    assert agents.AGENTS["opencode"].windows_argv("ses_abc") == ()


def test_wt_escapes_native_path_semicolons() -> None:
    job = terminals.Job("name", "D:/work", "opencode --session ses_abc",
                        ("D:/tools;apps/opencode.exe", "--session", "ses_abc"))
    argv = terminals.WindowsTerminal().plan([job]).commands[0]
    assert argv[-3:] == [r"D:/tools\;apps/opencode.exe", "--session", "ses_abc"]
