"""The hooks every session already runs register it and hand it its mail."""

import datetime
import io
import json
import subprocess
import sys
import time

import pytest

from core import hooks, mail, sessions
from core.lib import lock


@pytest.fixture(autouse=True)
def _no_project_from_environment(monkeypatch, tmp_path):
    """The suite may run inside a session whose project has `.eos`; a root-less
    test must not fall back to it, nor read the developer's own Claude names."""
    for name in (
        "CLAUDE_PROJECT_DIR",
        "EOS_PROJECT_DIR",
        "EOS_HOOK_STATE_DIR",
        "EOS_SESSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EOS_CLAUDE_SESSIONS_DIR", str(tmp_path / "claude-sessions"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".eos").mkdir(parents=True)
    return root


@pytest.fixture
def quiet(monkeypatch):
    """The project's own handlers out of the way: only the coordination path speaks."""
    for event in ("session-start", "user-prompt", "post-tool", "session-end"):
        monkeypatch.setitem(hooks.HANDLERS, event, lambda root, hook, cfg, agent: "")


def _display_name(tmp_path, session, name):
    folder = tmp_path / "claude-sessions"
    folder.mkdir(exist_ok=True)
    (folder / "4242.json").write_text(json.dumps({"sessionId": session, "name": name}))


def _run(monkeypatch, capsys, event, payload, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([event, *extra])
    assert code == 0
    return capsys.readouterr().out


def _claude(cwd, event_name, session="s1", **fields):
    return {
        "session_id": session,
        "cwd": str(cwd),
        "hook_event_name": event_name,
        "transcript_path": "/t/s1.jsonl",
        **fields,
    }


def _context(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _sender():
    sessions.register(
        "sender-1",
        agent="claude",
        host="",
        cwd="/w/other",
        project_root="/w/other",
        branch="main",
        display_name="writer",
    )


def test_session_start_registers_and_names(
    project, tmp_path, monkeypatch, capsys, quiet
):
    subprocess.run(["git", "init", "-q", "-b", "PROJ-123", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    _display_name(tmp_path, "s1", "nexus-d0")
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart", source="startup"),
        "--agent",
        "claude",
    )
    record = sessions.load("s1")
    assert (record.name, record.agent, record.host) == ("nexus-d0", "claude", "")
    assert (record.project_root, record.cwd) == (str(project), str(project))
    assert record.branch == "PROJ-123" and record.ended_at is None


def test_user_prompt_delivers_mail_as_additional_context(
    project, monkeypatch, capsys, quiet
):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _sender()
    mail.send("sender-1", "claude-s1", "the build is green")
    out = _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="hi"),
        "--agent",
        "claude",
    )
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "UserPromptSubmit"
    assert data["additionalContext"].startswith(mail.FRAME)
    assert "the build is green" in data["additionalContext"]
    assert mail.undelivered("s1") == []
    assert (
        _run(
            monkeypatch,
            capsys,
            "user-prompt",
            _claude(project, "UserPromptSubmit", prompt="again"),
            "--agent",
            "claude",
        )
        == ""
    )


def test_post_tool_delivers_mail(project, monkeypatch, capsys, quiet):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _sender()
    mail.send("sender-1", "claude-s1", "PROJ-123 is merged")
    out = _run(
        monkeypatch,
        capsys,
        "post-tool",
        _claude(
            project, "PostToolUse", tool_name="Read", tool_input={"file_path": "/a"}
        ),
        "--agent",
        "claude",
    )
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "PostToolUse"
    assert (
        mail.FRAME in data["additionalContext"]
        and "PROJ-123 is merged" in data["additionalContext"]
    )


def test_existing_context_merged_not_replaced(project, monkeypatch, capsys, quiet):
    monkeypatch.setitem(
        hooks.HANDLERS,
        "user-prompt",
        lambda root, hook, cfg, agent: hooks._context(
            "UserPromptSubmit", "the task brief"
        ),
    )
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _sender()
    mail.send("sender-1", "claude-s1", "hello")
    text = _context(
        _run(
            monkeypatch,
            capsys,
            "user-prompt",
            _claude(project, "UserPromptSubmit", prompt="go"),
            "--agent",
            "claude",
        )
    )
    assert text.startswith("the task brief")
    assert mail.FRAME in text and "hello" in text


def test_with_mail_keeps_every_kind_of_output():
    assert _context(hooks._with_mail("", "PostToolUse", "mail")) == "mail"
    assert (
        hooks._with_mail("plain words", "UserPromptSubmit", "mail")
        == "plain words\n\nmail"
    )
    decision = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": "short",
            }
        }
    )
    merged = json.loads(hooks._with_mail(decision, "PostToolUse", "mail"))[
        "hookSpecificOutput"
    ]
    assert merged == {
        "hookEventName": "PostToolUse",
        "updatedToolOutput": "short",
        "additionalContext": "mail",
    }
    top = json.loads(
        hooks._with_mail(json.dumps({"continue": True}), "PostToolUse", "mail")
    )
    assert (
        top["continue"] is True
        and top["hookSpecificOutput"]["additionalContext"] == "mail"
    )


def test_subagent_hook_neither_registers_nor_consumes(
    project, monkeypatch, capsys, quiet
):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _sender()
    mail.send("sender-1", "claude-s1", "for the main session")
    seen = sessions.load("s1").last_seen
    out = _run(
        monkeypatch,
        capsys,
        "post-tool",
        _claude(
            project,
            "PostToolUse",
            agent_id="sub-1",
            agent_type="Explore",
            tool_name="Read",
        ),
        "--agent",
        "claude",
    )
    assert out == ""
    assert [m.body for m in mail.undelivered("s1")] == ["for the main session"]
    assert sessions.load("s1").last_seen == seen
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart", session="s2", agent_id="sub-2"),
        "--agent",
        "claude",
    )
    assert sessions.load("s2") is None


def test_rootless_cwd_still_registers_and_delivers(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hooks.project_root(plain) is None
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(plain, "SessionStart"),
        "--agent",
        "claude",
    )
    record = sessions.load("s1")
    assert record.project_root is None and record.cwd == str(plain)
    _sender()
    mail.send("sender-1", "claude-s1", "no project here")
    text = _context(
        _run(
            monkeypatch,
            capsys,
            "user-prompt",
            _claude(plain, "UserPromptSubmit", prompt="x"),
            "--agent",
            "claude",
        )
    )
    assert text.startswith(mail.FRAME) and "no project here" in text


def test_devin_payload_registers_agent_devin(project, monkeypatch, capsys, quiet):
    payload = {"session_id": "dv1-session", "prompt_id": "p1", "cwd": str(project)}
    _run(monkeypatch, capsys, "session-start", payload, "--host", "devin")
    record = sessions.load("dv1-session")
    assert (record.agent, record.host, record.name) == (
        "devin",
        "devin",
        "devin-dv1-se",
    )
    _sender()
    mail.send("sender-1", "devin-dv1-se", "for devin")
    text = _context(
        _run(
            monkeypatch,
            capsys,
            "user-prompt",
            {**payload, "prompt": "go"},
            "--host",
            "devin",
        )
    )
    assert "for devin" in text


def test_sessions_off_does_nothing(project, monkeypatch, capsys, quiet):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _sender()
    mail.send("sender-1", "claude-s1", "unseen")
    monkeypatch.setenv("EOS_SESSIONS", "off")
    assert (
        _run(
            monkeypatch,
            capsys,
            "user-prompt",
            _claude(project, "UserPromptSubmit", prompt="x"),
            "--agent",
            "claude",
        )
        == ""
    )
    assert [m.body for m in mail.undelivered("s1")] == ["unseen"]
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart", session="s9"),
        "--agent",
        "claude",
    )
    assert sessions.load("s9") is None


def test_session_end_marks_ended(project, monkeypatch, capsys, quiet):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    _run(
        monkeypatch,
        capsys,
        "session-end",
        _claude(project, "SessionEnd", reason="exit"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").ended_at is not None
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart", source="resume"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").ended_at is None


def test_prompt_of_a_session_older_than_the_feature_registers_it(
    project, monkeypatch, capsys, quiet
):
    _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="x"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").name == "claude-s1"


def test_prompt_adopts_a_display_name_written_after_start(
    project, tmp_path, monkeypatch, capsys, quiet
):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").name == "claude-s1"
    sessions.register(
        "s2",
        agent="claude",
        host="",
        cwd="/w",
        project_root=None,
        branch=None,
        display_name="nexus-d0",
    )
    _display_name(tmp_path, "s1", "nexus-d0")
    _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="x"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").name == "nexus-d0-2"


def test_prompt_keeps_a_name_the_user_chose(
    project, tmp_path, monkeypatch, capsys, quiet
):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    sessions.set_name("s1", "reviewer")
    _display_name(tmp_path, "s1", "nexus-d0")
    _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="x"),
        "--agent",
        "claude",
    )
    assert sessions.load("s1").name == "reviewer"


def test_recent_refresh_runs_no_git(project, monkeypatch, capsys, quiet):
    _run(
        monkeypatch,
        capsys,
        "session-start",
        _claude(project, "SessionStart"),
        "--agent",
        "claude",
    )
    calls = []
    monkeypatch.setattr(hooks, "_session_branch", lambda cwd: calls.append(cwd))
    _run(
        monkeypatch,
        capsys,
        "post-tool",
        _claude(project, "PostToolUse", tool_name="Read"),
        "--agent",
        "claude",
    )
    assert calls == []


def test_mail_failure_keeps_the_handler_output(project, monkeypatch, capsys, quiet):
    monkeypatch.setitem(
        hooks.HANDLERS,
        "user-prompt",
        lambda root, hook, cfg, agent: hooks._context(
            "UserPromptSubmit", "the task brief"
        ),
    )

    def jammed(session, **kwargs):
        raise lock.LockTimeout("held")

    monkeypatch.setattr(mail, "delivery_text", jammed)
    out = _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="x"),
        "--agent",
        "claude",
    )
    assert _context(out) == "the task brief"


def _briefed(monkeypatch):
    monkeypatch.setitem(
        hooks.HANDLERS,
        "user-prompt",
        lambda root, hook, cfg, agent: hooks._context("UserPromptSubmit", "the task brief"),
    )


def _timed_prompt(monkeypatch, capsys, project, session):
    began = time.monotonic()
    out = _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", session=session, prompt="x"),
        "--agent",
        "claude",
    )
    return out, time.monotonic() - began


def test_held_registry_lock_skips_coordination_fast(project, monkeypatch, capsys, quiet):
    _briefed(monkeypatch)
    with lock.locked(sessions._dir()):
        out, took = _timed_prompt(monkeypatch, capsys, project, "s7")
    assert took < 2.0
    assert _context(out) == "the task brief"
    assert sessions.load("s7") is None


def test_held_mail_lock_skips_delivery_fast(project, monkeypatch, capsys, quiet):
    _run(monkeypatch, capsys, "session-start", _claude(project, "SessionStart"), "--agent", "claude")
    _sender()
    mail.send("sender-1", "claude-s1", "waits for the next hook")
    _briefed(monkeypatch)
    with lock.locked(mail._delivered_path("s1")):
        out, took = _timed_prompt(monkeypatch, capsys, project, "s1")
    assert took < 2.0
    assert _context(out) == "the task brief"
    assert [m.body for m in mail.undelivered("s1")] == ["waits for the next hook"]


def test_devin_post_tool_refreshes_but_keeps_mail_for_the_prompt(
    project, monkeypatch, capsys, quiet
):
    payload = {"session_id": "dv1-session", "prompt_id": "p1", "cwd": str(project)}
    _run(monkeypatch, capsys, "session-start", payload, "--host", "devin")
    path = sessions._path("dv1-session")
    data = json.loads(path.read_text())
    data["last_seen"] = sessions._stamp(sessions._now() - datetime.timedelta(minutes=5))
    path.write_text(json.dumps(data))
    _sender()
    mail.send("sender-1", "devin-dv1-se", "for devin")
    out = _run(
        monkeypatch,
        capsys,
        "post-tool",
        {**payload, "tool_name": "read", "tool_input": {"file_path": "/a"}},
        "--host",
        "devin",
    )
    assert "for devin" not in out
    assert [m.body for m in mail.undelivered("dv1-session")] == ["for devin"]
    assert sessions.load("dv1-session").last_seen != data["last_seen"]
    text = _context(
        _run(monkeypatch, capsys, "user-prompt", {**payload, "prompt": "go"}, "--host", "devin")
    )
    assert "for devin" in text
    assert mail.undelivered("dv1-session") == []


def _clipping(monkeypatch, chars):
    """A post-tool handler that rewrites a Bash output of `chars` characters."""
    output = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": {"stdout": "x" * chars, "stderr": ""},
            }
        }
    )
    monkeypatch.setitem(hooks.HANDLERS, "post-tool", lambda root, hook, cfg, agent: output)


def test_clipped_tool_output_does_not_shrink_the_mail_room(
    project, monkeypatch, capsys, quiet
):
    _run(monkeypatch, capsys, "session-start", _claude(project, "SessionStart"), "--agent", "claude")
    _sender()
    body = "m" * 2000
    mail.send("sender-1", "claude-s1", body)
    _clipping(monkeypatch, 20_000)
    out = _run(
        monkeypatch,
        capsys,
        "post-tool",
        _claude(project, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}),
        "--agent",
        "claude",
    )
    data = json.loads(out)["hookSpecificOutput"]
    assert data["updatedToolOutput"]["stdout"] == "x" * 20_000
    assert body in data["additionalContext"] and "[cut:" not in data["additionalContext"]
    assert mail.undelivered("s1") == []


def test_no_room_left_keeps_mail_for_a_later_hook(project, monkeypatch, capsys, quiet):
    _run(monkeypatch, capsys, "session-start", _claude(project, "SessionStart"), "--agent", "claude")
    _sender()
    mail.send("sender-1", "claude-s1", "next time")
    brief = "b" * (hooks.HOOK_TEXT_CHARS - hooks.MAIL_MARGIN_CHARS - hooks.MAIL_MIN_CHARS + 1)
    monkeypatch.setitem(
        hooks.HANDLERS,
        "user-prompt",
        lambda root, hook, cfg, agent: hooks._context("UserPromptSubmit", brief),
    )
    out = _run(
        monkeypatch,
        capsys,
        "user-prompt",
        _claude(project, "UserPromptSubmit", prompt="x"),
        "--agent",
        "claude",
    )
    assert _context(out) == brief
    assert [m.body for m in mail.undelivered("s1")] == ["next time"]
