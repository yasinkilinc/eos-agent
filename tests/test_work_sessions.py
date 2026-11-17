"""The link between the work ledger and the session registry.

A claim points the claiming session's registry record at the item, so another
session can address it by the item; a message that reaches a session through
its work item (or with --log) leaves a `log` event on that item, so the durable
part of the conversation survives the session and travels with git.
"""

import pytest

from core import eos, mail, sessions, work

ME = "sess-me"


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    (root / ".eos").mkdir(parents=True)
    return root


def _register(session, name, project, **overrides):
    fields = dict(
        agent="claude",
        host="local",
        cwd=str(project),
        project_root=str(project),
        branch="main",
        display_name=name,
    )
    fields.update(overrides)
    return sessions.register(session, **fields)


@pytest.fixture
def project(tmp_path):
    return _project(tmp_path)


@pytest.fixture(autouse=True)
def me(monkeypatch, project):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ME)
    return _register(ME, "mine", project)


def _run(capsys, *argv):
    code = eos.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _log_events(project, item_id):
    return [e for e in work.load(project) if e.id == item_id and e.event == work.EVENT_LOG]


def test_work_claim_sets_registry_work_item(capsys, project):
    entry = work.open_item(project, "PROJ-123 order intake")
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id)
    assert code == 0
    assert sessions.load(ME).work_item == {
        "id": entry.id,
        "title": "PROJ-123 order intake",
        "project": str(project.resolve()),
    }


def test_work_claim_with_explicit_session_flag_sets_that_session(capsys, project):
    other = _register("sess-b", "beta", project)
    entry = work.open_item(project, "PROJ-123 order intake")
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id, "--session", other.id)
    assert code == 0
    assert sessions.load(other.id).work_item["id"] == entry.id
    assert sessions.load(ME).work_item is None


def test_work_add_claim_sets_registry_work_item(capsys, project):
    code, out, _ = _run(
        capsys, "work", "add", str(project), "--title", "PROJ-456 refund flow", "--claim"
    )
    assert code == 0
    record = sessions.load(ME)
    assert record.work_item["title"] == "PROJ-456 refund flow"
    assert out.startswith(record.work_item["id"])


def test_work_add_without_claim_leaves_registry_alone(capsys, project):
    code, _, _ = _run(capsys, "work", "add", str(project), "--title", "PROJ-456 refund flow")
    assert code == 0
    assert sessions.load(ME).work_item is None


def test_work_claim_without_registered_session_still_succeeds(capsys, project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-unregistered")
    entry = work.open_item(project, "PROJ-123 order intake")
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id)
    assert code == 0
    assert sessions.load("sess-unregistered") is None


def test_work_claim_survives_a_registry_failure(capsys, project, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("registry unavailable")

    monkeypatch.setattr(sessions, "set_work_item", broken)
    entry = work.open_item(project, "PROJ-123 order intake")
    code, _, err = _run(capsys, "work", "claim", str(project), entry.id)
    assert code == 0
    assert "registry unavailable" not in err
    assert work.items(project)[0].status == work.ACTIVE


def test_msg_send_to_work_key_logs_on_item(capsys, tmp_path):
    shop = _project(tmp_path, "shop")
    entry = work.open_item(shop, "PROJ-123 order intake")
    other = _register("sess-b", "beta", shop, agent="devin")
    sessions.set_work_item(other.id, entry.id, "PROJ-123 order intake", ticket="PROJ-123")

    code, _, _ = _run(capsys, "msg", "send", "--to", "PROJ-123", "the gateway moved")
    assert code == 0
    assert [m.body for m in mail.inbox(other.id)] == ["the gateway moved"]
    events = _log_events(shop, entry.id)
    assert len(events) == 1
    assert events[0].body == "msg from mine: the gateway moved"
    assert events[0].session == ME
    assert events[0].agent == "claude"


def test_msg_send_to_name_without_log_flag_writes_no_ledger_event(capsys, tmp_path):
    shop = _project(tmp_path, "shop")
    entry = work.open_item(shop, "PROJ-123 order intake")
    other = _register("sess-b", "beta", shop)
    sessions.set_work_item(other.id, entry.id, "PROJ-123 order intake")

    code, _, _ = _run(capsys, "msg", "send", "--to", "beta", "just chatting")
    assert code == 0
    assert _log_events(shop, entry.id) == []


def test_msg_send_log_flag_on_name_target_logs_on_recipient_item(capsys, tmp_path):
    shop = _project(tmp_path, "shop")
    entry = work.open_item(shop, "PROJ-123 order intake")
    other = _register("sess-b", "beta", shop)
    sessions.set_work_item(other.id, entry.id, "PROJ-123 order intake")

    code, _, _ = _run(capsys, "msg", "send", "--to", "beta", "--log", "decision: keep v2")
    assert code == 0
    assert [e.body for e in _log_events(shop, entry.id)] == ["msg from mine: decision: keep v2"]


def test_msg_send_log_flag_without_recipient_item_notes_and_still_sends(capsys, tmp_path):
    shop = _project(tmp_path, "shop")
    other = _register("sess-b", "beta", shop)

    code, out, err = _run(capsys, "msg", "send", "--to", "beta", "--log", "hello")
    assert code == 0
    assert "sent to beta" in out
    assert "no work item" in err
    assert [m.body for m in mail.inbox(other.id)] == ["hello"]
    assert work.load(shop) == []


def test_msg_send_log_without_project_root_notes_and_still_sends(capsys, tmp_path):
    other = _register("sess-b", "beta", tmp_path, project_root=None)
    sessions.set_work_item(other.id, "PROJ-123-order-intake", "PROJ-123 order intake")

    code, out, err = _run(capsys, "msg", "send", "--to", "beta", "--log", "hello")
    assert code == 0
    assert "sent to beta" in out
    assert "no project" in err
    assert [m.body for m in mail.inbox(other.id)] == ["hello"]


def test_msg_send_survives_a_ledger_failure(capsys, tmp_path, monkeypatch):
    shop = _project(tmp_path, "shop")
    entry = work.open_item(shop, "PROJ-123 order intake")
    other = _register("sess-b", "beta", shop)
    sessions.set_work_item(other.id, entry.id, "PROJ-123 order intake", ticket="PROJ-123")

    def broken(*args, **kwargs):
        raise OSError("ledger is read-only")

    monkeypatch.setattr(work, "append", broken)
    code, out, err = _run(capsys, "msg", "send", "--to", "PROJ-123", "hello")
    assert code == 0
    assert "sent to beta" in out
    assert "ledger is read-only" in err
    assert [m.body for m in mail.inbox(other.id)] == ["hello"]


def test_msg_send_logs_on_the_ledger_the_item_was_claimed_in(capsys, tmp_path):
    home = _project(tmp_path, "home")
    sibling = _project(tmp_path, "sibling")
    entry = work.open_item(sibling, "PROJ-123 order intake")
    other = _register("sess-b", "beta", home)
    code, _, _ = _run(
        capsys, "work", "claim", str(sibling), entry.id, "--session", other.id
    )
    assert code == 0
    assert sessions.load(other.id).project_root == str(home)

    code, _, _ = _run(capsys, "msg", "send", "--to", entry.id, "the gateway moved")
    assert code == 0
    assert [e.body for e in _log_events(sibling, entry.id)] == [
        "msg from mine: the gateway moved"
    ]
    assert work.load(home) == []


def test_msg_send_falls_back_to_project_root_for_a_record_without_project(capsys, tmp_path):
    shop = _project(tmp_path, "shop")
    entry = work.open_item(shop, "PROJ-123 order intake")
    other = _register("sess-b", "beta", shop)
    sessions.set_work_item(other.id, entry.id, "PROJ-123 order intake")
    assert "project" not in sessions.load(other.id).work_item

    code, _, _ = _run(capsys, "msg", "send", "--to", "beta", "--log", "hello")
    assert code == 0
    assert len(_log_events(shop, entry.id)) == 1


def test_claim_points_the_session_at_the_ticket_not_the_title_word(capsys, project):
    entry = work.open_item(project, "Review the login flow", ticket="PROJ-123")
    other = _register("sess-b", "beta", project)
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id, "--session", other.id)
    assert code == 0
    assert sessions.load(other.id).work_item["ticket"] == "PROJ-123"
    assert sessions.resolve("PROJ-123", sender=ME).id == other.id
    assert sessions.resolve(entry.id, sender=ME).id == other.id
    with pytest.raises(sessions.NoMatch):
        sessions.resolve("review", sender=ME)


def test_work_add_claim_stores_the_ticket(capsys, project):
    code, _, _ = _run(
        capsys, "work", "add", str(project), "--title", "refund flow",
        "--ticket", "PROJ-456", "--claim",
    )
    assert code == 0
    assert sessions.load(ME).work_item["ticket"] == "PROJ-456"


@pytest.mark.parametrize("command", ["done", "drop"])
def test_closing_the_item_clears_the_session_pointer(capsys, project, command):
    entry = work.open_item(project, "refund flow", ticket="PROJ-123")
    other = _register("sess-b", "beta", project)
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id, "--session", other.id)
    assert code == 0 and sessions.load(other.id).work_item["id"] == entry.id
    flag = "--note" if command == "done" else "--reason"
    code, _, _ = _run(
        capsys, "work", command, str(project), entry.id, flag, "closed", "--session", other.id
    )
    assert code == 0
    assert sessions.load(other.id).work_item is None
    code, _, err = _run(capsys, "msg", "send", "--to", entry.id, "still there?")
    assert code == 2 and "no live session matches" in err
    code, _, _ = _run(capsys, "msg", "send", "--to", "PROJ-123", "still there?")
    assert code == 2
    assert mail.inbox(other.id) == []


def test_closing_another_item_keeps_the_session_pointer(capsys, project):
    held = work.open_item(project, "refund flow")
    other_item = work.open_item(project, "order intake")
    code, _, _ = _run(capsys, "work", "claim", str(project), held.id)
    assert code == 0
    code, _, _ = _run(capsys, "work", "claim", str(project), other_item.id, "--session", "sess-x")
    assert code == 0
    code, _, _ = _run(capsys, "work", "done", str(project), other_item.id, "--session", "sess-x")
    assert code == 0
    assert sessions.load(ME).work_item["id"] == held.id


def test_work_done_survives_a_registry_failure(capsys, project, monkeypatch):
    entry = work.open_item(project, "refund flow")
    code, _, _ = _run(capsys, "work", "claim", str(project), entry.id)
    assert code == 0

    def broken(*args, **kwargs):
        raise OSError("registry unavailable")

    monkeypatch.setattr(sessions, "clear_work_item", broken)
    code, _, err = _run(capsys, "work", "done", str(project), entry.id)
    assert code == 0
    assert "registry unavailable" not in err
    assert work.items(project, status="all")[0].status == work.DONE
