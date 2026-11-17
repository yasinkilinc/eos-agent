"""The machine-wide session registry: who is live, what they are called, how a
target resolves to exactly one of them."""

import datetime
import json

import pytest

from core import sessions


def _register(session, **overrides):
    fields = dict(
        agent="claude",
        host="local",
        cwd="/w",
        project_root="/w",
        branch="main",
        display_name=None,
    )
    fields.update(overrides)
    return sessions.register(session, **fields)


def _age(session, **delta):
    """Rewrite a record's last_seen to `delta` ago."""
    path = sessions._path(session)
    data = json.loads(path.read_text())
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)
    data["last_seen"] = then.isoformat()
    path.write_text(json.dumps(data))


def test_register_uses_display_name():
    assert _register("s1", display_name="nexus-94").name == "nexus-94"


def test_duplicate_default_name_gets_suffix():
    _register("s1", display_name="nexus-94")
    assert _register("s2", display_name="nexus-94").name == "nexus-94-2"
    assert _register("s3", display_name="nexus-94").name == "nexus-94-3"


def test_default_name_without_display_name():
    assert (
        _register("abcdef123", agent="devin", display_name=None).name == "devin-abcdef"
    )


def test_register_refresh_keeps_name_and_started_at():
    first = _register("s1", display_name="nexus-94")
    again = _register("s1", display_name="other", cwd="/elsewhere", branch="dev")
    assert again.name == "nexus-94"
    assert again.started_at == first.started_at
    assert (again.cwd, again.branch) == ("/elsewhere", "dev")


def test_register_after_end_makes_session_live_again():
    _register("s1", display_name="a")
    sessions.end("s1")
    _register("s1")
    assert [r.id for r in sessions.live()] == ["s1"]
    assert sessions.load("s1").ended_at is None


def test_ended_session_not_live():
    _register("s1", display_name="a")
    sessions.end("s1")
    assert sessions.live() == []
    assert sessions.load("s1").ended_at is not None
    assert [r.id for r in sessions.all_records()] == ["s1"]


def test_stale_session_not_live():
    _register("s1", display_name="a")
    _age("s1", hours=3)
    assert sessions.live() == []


def test_live_window_is_configurable(monkeypatch):
    _register("s1", display_name="a")
    _age("s1", hours=3)
    monkeypatch.setenv("EOS_SESSIONS_LIVE_HOURS", "4")
    assert [r.id for r in sessions.live()] == ["s1"]


def test_touch_skips_fresh_write_and_refreshes_old_one():
    _register("s1", display_name="a", cwd="/one")
    sessions.touch("s1", cwd="/two", branch="x")
    assert sessions.load("s1").cwd == "/one"
    _age("s1", minutes=5)
    sessions.touch("s1", cwd="/two", branch="x")
    record = sessions.load("s1")
    assert (record.cwd, record.branch) == ("/two", "x")
    assert sessions.live()


def test_touch_unknown_session_writes_nothing():
    sessions.touch("ghost", cwd="/x", branch=None)
    assert sessions.load("ghost") is None


def test_set_name_taken_by_live_raises():
    _register("s1", display_name="a")
    _register("s2", display_name="b")
    with pytest.raises(sessions.NameTaken) as raised:
        sessions.set_name("s2", "a")
    assert raised.value.holder.id == "s1"


def test_set_name_of_ended_reused():
    _register("s1", display_name="a")
    _register("s2", display_name="b")
    sessions.end("s1")
    assert sessions.set_name("s2", "a").name == "a"


def test_set_name_to_own_name_is_fine():
    _register("s1", display_name="a")
    assert sessions.set_name("s1", "a").name == "a"


def test_resolve_by_name():
    _register("s1", display_name="nexus-d0")
    _register("s2", display_name="nexus-94")
    assert sessions.resolve("nexus-d0", sender="s2").id == "s1"


def test_resolve_by_work_item():
    _register("s1", display_name="a")
    sessions.set_work_item("s1", "k-7f3a", "top-up flow", ticket="PROJ-123")
    assert sessions.load("s1").work_item == {
        "id": "k-7f3a",
        "title": "top-up flow",
        "ticket": "PROJ-123",
    }
    assert sessions.resolve("PROJ-123", sender=None).id == "s1"
    assert sessions.resolve("proj-123", sender=None).id == "s1"
    assert sessions.resolve("k-7f3a", sender=None).id == "s1"


def test_title_first_word_is_not_a_work_key():
    _register("s1", display_name="a")
    sessions.set_work_item("s1", "k-1", "Review the login flow", ticket="PROJ-123")
    with pytest.raises(sessions.NoMatch):
        sessions.resolve("review", sender=None)
    assert sessions.resolve("PROJ-123", sender=None).id == "s1"
    assert sessions.resolve("k-1", sender=None).id == "s1"


def test_work_item_without_ticket_resolves_by_id_only():
    _register("s1", display_name="a")
    path = sessions._path("s1")
    data = json.loads(path.read_text())
    data["work_item"] = {"id": "k-1", "title": "PROJ-123 order intake"}
    path.write_text(json.dumps(data))
    assert sessions.resolve("k-1", sender=None).id == "s1"
    with pytest.raises(sessions.NoMatch):
        sessions.resolve("PROJ-123", sender=None)


def test_clear_work_item_only_when_it_points_at_that_item():
    _register("s1", display_name="a")
    sessions.set_work_item("s1", "k-1", "one")
    sessions.clear_work_item("s1", "k-2")
    assert sessions.load("s1").work_item["id"] == "k-1"
    sessions.clear_work_item("s1", "k-1")
    assert sessions.load("s1").work_item is None
    sessions.clear_work_item("never-registered", "k-1")
    assert sessions.load("never-registered") is None


def test_record_with_an_unknown_field_is_read():
    _register("s1", display_name="a")
    path = sessions._path("s1")
    data = json.loads(path.read_text())
    data["added_by_a_newer_eos"] = {"x": 1}
    path.write_text(json.dumps(data))
    assert sessions.load("s1").name == "a"
    assert [r.id for r in sessions.live()] == ["s1"]


def test_resolve_by_id_prefix():
    _register("abc123", display_name="a")
    _register("abd456", display_name="b")
    assert sessions.resolve("abc", sender=None).id == "abc123"
    assert sessions.resolve("abd456", sender=None).id == "abd456"


def test_resolve_ambiguous_lists_candidates():
    _register("s1", display_name="a")
    _register("s2", display_name="b")
    sessions.set_work_item("s1", "k-1", "one", ticket="PROJ-123")
    sessions.set_work_item("s2", "k-2", "two", ticket="PROJ-123")
    with pytest.raises(sessions.Ambiguous) as raised:
        sessions.resolve("PROJ-123", sender=None)
    assert {r.id for r in raised.value.candidates} == {"s1", "s2"}


def test_resolve_ambiguous_id_prefix():
    _register("abc1", display_name="a")
    _register("abc2", display_name="b")
    with pytest.raises(sessions.Ambiguous):
        sessions.resolve("abc", sender=None)


def test_resolve_none_lists_live():
    _register("s1", display_name="a")
    _register("s2", display_name="b")
    sessions.end("s2")
    with pytest.raises(sessions.NoMatch) as raised:
        sessions.resolve("nobody", sender="s1")
    assert [r.id for r in raised.value.live] == ["s1"]


def test_resolve_ignores_ended_sessions():
    _register("s1", display_name="a")
    sessions.end("s1")
    with pytest.raises(sessions.NoMatch):
        sessions.resolve("a", sender=None)


def test_resolve_self_refused():
    _register("s1", display_name="a")
    with pytest.raises(sessions.SelfTarget):
        sessions.resolve("a", sender="s1")


def test_claude_display_name_reads_sessions_dir(tmp_path, monkeypatch):
    folder = tmp_path / "claude-sessions"
    folder.mkdir()
    (folder / "100.json").write_text(json.dumps({"sessionId": "other", "name": "x"}))
    (folder / "101.json").write_text("not json")
    (folder / "102.json").write_text(
        json.dumps({"sessionId": "s1", "name": "nexus-d0"})
    )
    monkeypatch.setenv("EOS_CLAUDE_SESSIONS_DIR", str(folder))
    assert sessions.claude_display_name("s1") == "nexus-d0"
    assert sessions.claude_display_name("missing") is None


def test_claude_display_name_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_CLAUDE_SESSIONS_DIR", str(tmp_path / "nope"))
    assert sessions.claude_display_name("s1") is None


def test_atomic_write_survives_corrupt_file():
    _register("s1", display_name="a")
    (sessions._dir() / "s2.json").write_text("{garbage")
    assert [r.id for r in sessions.live()] == ["s1"]
    assert sessions.load("s2") is None
    assert [r.id for r in sessions.all_records()] == ["s1"]


@pytest.mark.parametrize(
    "broken",
    [{"last_seen": 123}, {"work_item": "PROJ-123"}, {"name": None}, {"branch": 5}],
)
def test_wrong_typed_record_is_skipped_like_a_corrupt_file(broken):
    _register("s1", display_name="a")
    _register("s2", display_name="b")
    path = sessions._path("s2")
    data = json.loads(path.read_text())
    data.update(broken)
    path.write_text(json.dumps(data))
    assert [r.id for r in sessions.live()] == ["s1"]
    assert sessions.load("s2") is None
    with pytest.raises(sessions.NoMatch):
        sessions.resolve("b", sender=None)


def test_prune_removes_old_ended_and_stale_only():
    _register("old-ended", display_name="a")
    sessions.end("old-ended")
    _register("old-stale", display_name="b")
    _register("recent-ended", display_name="c")
    sessions.end("recent-ended")
    _register("live", display_name="d")
    for session in ("old-ended", "old-stale"):
        path = sessions._path(session)
        data = json.loads(path.read_text())
        old = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=15)
        ).isoformat()
        data["last_seen"] = old
        if data["ended_at"]:
            data["ended_at"] = old
        path.write_text(json.dumps(data))
    assert sessions.prune() == 2
    assert {r.id for r in sessions.all_records()} == {"recent-ended", "live"}


def test_session_id_is_sanitised_for_the_file_name():
    _register("../evil/id", display_name="a")
    assert [p.parent for p in sessions._dir().glob("*.json")] == [sessions._dir()]
    assert sessions.load("../evil/id").id == "../evil/id"


def test_enabled_switch(monkeypatch):
    assert sessions.enabled()
    monkeypatch.setenv("EOS_SESSIONS", "off")
    assert not sessions.enabled()


def test_set_work_item_stores_the_project_when_given():
    sessions.register("s1", agent="claude", host="h", cwd="/w/a", project_root="/w/a", branch=None)
    sessions.set_work_item("s1", "k-1", "PROJ-123 one", "/w/b")
    assert sessions.load("s1").work_item == {"id": "k-1", "title": "PROJ-123 one", "project": "/w/b"}
