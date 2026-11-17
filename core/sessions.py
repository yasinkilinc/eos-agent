"""Who is running on this machine right now, and what each is called.

Sessions on one machine cannot see each other. The user knows them by the
names the harness shows (`nexus-d0`), by the ticket one is working on, or by
nothing at all, and every question of the form "tell the session doing X"
ends with the user as the courier. This is the registry that makes X
resolvable: one record per session, written by the hooks the session already
runs, read by anything that wants to address one of them.

Three properties decide the shape.

**One file per session.** A record is rewritten whole, through a temp file and
a rename, and only by the session it describes (plus `set_name`, which the user
runs). Two sessions never write the same file, so a reader sees the old record
or the new one and a half-written or garbage file is one session missing from
the list, not the registry gone. Readers skip what they cannot parse.

**Live is a window, not a flag.** A session killed with its terminal never runs
its end hook. So "live" is "not ended and seen within the last
`EOS_SESSIONS_LIVE_HOURS` (default 2)", and a record nobody refreshed simply
ages out. Ended and aged-out records stay as history until `prune` drops them.

**A target resolves to exactly one session or to none.** Zero matches, more
than one, and yourself are three different refusals, each carrying what the
caller needs to choose again. Nothing here picks "the first" or "all": a
message delivered to a guess is delivered to the wrong session, and the
sender never learns.

Names are unique among live sessions only. A default that collides gets a
numeric suffix; an explicit name that collides is refused, naming the holder.
The name of an ended session is free to take again.

The registry knows no workspace: a work item is an id, a title and an optional
ticket the caller supplies, and a "work key" is the item id or its ticket --
never a word of the title, which would let "Review the login flow" answer to
`review`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path

from core.lib import atomic, lock

OFF_ENV = "EOS_SESSIONS"
LIVE_HOURS_ENV = "EOS_SESSIONS_LIVE_HOURS"
CLAUDE_SESSIONS_ENV = "EOS_CLAUDE_SESSIONS_DIR"

DEFAULT_LIVE_HOURS = 2.0
# Ended and aged-out records are history; after this they are only clutter.
PRUNE_AFTER_DAYS = 14
# A hook runs on every prompt and tool call; a refresh younger than this
# changes nothing a reader can use, so it is not written.
TOUCH_INTERVAL_SECONDS = 60


@dataclasses.dataclass
class Record:
    id: str
    name: str
    agent: str
    host: str
    project_root: str | None
    cwd: str
    branch: str | None
    work_item: dict | None
    started_at: str
    last_seen: str
    ended_at: str | None = None


class NameTaken(Exception):
    def __init__(self, holder: Record):
        super().__init__(
            f"the name {holder.name!r} is held by live session {holder.id}"
        )
        self.holder = holder


class NoMatch(Exception):
    def __init__(self, live: list[Record]):
        super().__init__("no live session matches")
        self.live = live


class Ambiguous(Exception):
    def __init__(self, candidates: list[Record]):
        super().__init__("more than one live session matches")
        self.candidates = candidates


class SelfTarget(Exception):
    pass


def enabled() -> bool:
    return os.environ.get(OFF_ENV, "").strip().lower() != "off"


# --- storage -----------------------------------------------------------------------


def _dir() -> Path:
    from core import executions

    return executions._state_dir() / "sessions" / "registry"


def _safe(session: str) -> str:
    return (
        "".join(ch for ch in session if ch.isalnum() or ch in "-_.")[:64] or "unnamed"
    )


def _path(session: str) -> Path:
    return _dir() / f"{_safe(session)}.json"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(moment: datetime.datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


_TEXT_FIELDS = ("id", "name", "agent", "host", "cwd", "started_at", "last_seen")
_OPTIONAL_TEXT_FIELDS = ("project_root", "branch", "ended_at")
_FIELDS = {field.name for field in dataclasses.fields(Record)}


def _read(path: Path) -> Record | None:
    """The record in `path`, or None for anything that is not one: unreadable,
    not JSON, or valid JSON whose fields are not the types a Record holds.
    Fields this version does not know are dropped: every EOS version on the
    machine shares the registry, and a newer one adding a field must not make
    this one lose the session."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        record = Record(**{k: v for k, v in data.items() if k in _FIELDS})
    except (OSError, ValueError, TypeError):
        return None
    if not all(isinstance(getattr(record, f), str) for f in _TEXT_FIELDS):
        return None
    if not all(getattr(record, f) is None or isinstance(getattr(record, f), str) for f in _OPTIONAL_TEXT_FIELDS):
        return None
    if record.work_item is not None and not isinstance(record.work_item, dict):
        return None
    return record


def _write(record: Record) -> None:
    atomic.write_text(
        _path(record.id), json.dumps(dataclasses.asdict(record), indent=2) + "\n"
    )


def _locked(timeout: float = lock.TIMEOUT_SECONDS):
    """One writer at a time across the registry: names are checked against the
    other records, so two registrations must not both see the name free.

    The lock is one for every session on the machine; a hook passes a short
    `timeout` so a stalled holder costs it a skipped refresh, not its budget."""
    return lock.locked(_dir(), timeout=timeout)


def load(session: str) -> Record | None:
    return _read(_path(session))


def all_records() -> list[Record]:
    folder = _dir()
    if not folder.is_dir():
        return []
    records = (_read(path) for path in sorted(folder.glob("*.json")))
    return [record for record in records if record is not None]


def _live_hours() -> float:
    try:
        return float(os.environ.get(LIVE_HOURS_ENV, DEFAULT_LIVE_HOURS))
    except ValueError:
        return DEFAULT_LIVE_HOURS


def _is_live(record: Record, now: datetime.datetime) -> bool:
    if record.ended_at:
        return False
    seen = _parse(record.last_seen)
    if seen is None:
        return False
    return (now - seen) <= datetime.timedelta(hours=_live_hours())


def live(now: datetime.datetime | None = None) -> list[Record]:
    now = now or _now()
    return [record for record in all_records() if _is_live(record, now)]


def prune(
    now: datetime.datetime | None = None, *, timeout: float = lock.TIMEOUT_SECONDS
) -> int:
    """Delete records that ended, or were last seen, more than PRUNE_AFTER_DAYS ago."""
    now = now or _now()
    limit = datetime.timedelta(days=PRUNE_AFTER_DAYS)
    removed = 0
    with _locked(timeout):
        for record in all_records():
            moment = _parse(record.ended_at or record.last_seen)
            if moment is None or now - moment <= limit:
                continue
            try:
                _path(record.id).unlink()
            except FileNotFoundError:
                continue
            removed += 1
    return removed


# --- names -------------------------------------------------------------------------


def claude_display_name(session: str) -> str | None:
    """The name Claude Code shows for `session`, from its own per-process files.

    Matched by `sessionId`, not by file name: the file is named after a pid."""
    configured = os.environ.get(CLAUDE_SESSIONS_ENV)
    folder = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".claude" / "sessions"
    )
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session:
            continue
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _live_holder(name: str, *, besides: str) -> Record | None:
    for record in live():
        if record.name == name and record.id != besides:
            return record
    return None


def _free_name(base: str, session: str) -> str:
    candidate, number = base, 1
    while _live_holder(candidate, besides=session):
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def _fallback_name(agent: str, session: str) -> str:
    return f"{agent}-{session[:6]}"


def is_fallback_name(record: Record) -> bool:
    """Whether `record` still carries the name `register` gives when the harness
    names nothing: `<agent>-<short id>`, suffixed or not."""
    base = _fallback_name(record.agent, record.id)
    suffix = record.name[len(base) :]
    return record.name.startswith(base) and (
        not suffix or (suffix[0] == "-" and suffix[1:].isdigit())
    )


# --- writes ------------------------------------------------------------------------


def register(
    session: str,
    *,
    agent: str,
    host: str,
    cwd: str,
    project_root: str | None,
    branch: str | None,
    display_name: str | None = None,
    timeout: float = lock.TIMEOUT_SECONDS,
) -> Record:
    """Create the record, or refresh an existing one and bring it back to life.

    The name is chosen once, on create: a refresh never renames, so a name the
    user set (or one another session already addressed) does not change under them."""
    with _locked(timeout):
        now = _stamp()
        record = load(session)
        if record is None:
            base = (display_name or "").strip() or _fallback_name(agent, session)
            record = Record(
                id=session,
                name=_free_name(base, session),
                agent=agent,
                host=host,
                project_root=project_root,
                cwd=cwd,
                branch=branch,
                work_item=None,
                started_at=now,
                last_seen=now,
            )
        else:
            record.agent, record.host = agent, host
            record.project_root, record.cwd, record.branch = project_root, cwd, branch
            record.last_seen, record.ended_at = now, None
        _write(record)
        return record


def touch_due(record: Record) -> bool:
    """Whether `touch` would write `record`: live, and not seen in the last minute.
    A caller asks before paying for what it would pass (the branch, a git call)."""
    if record.ended_at:
        return False
    seen = _parse(record.last_seen)
    return seen is None or (_now() - seen).total_seconds() >= TOUCH_INTERVAL_SECONDS


def touch(
    session: str,
    *,
    cwd: str,
    branch: str | None,
    timeout: float = lock.TIMEOUT_SECONDS,
) -> None:
    """Refresh where the session is. No-op for an unknown or ended session
    (only `register` brings one back) and for a record seen under a minute ago."""
    record = load(session)
    if record is None or not touch_due(record):
        return
    with _locked(timeout):
        record = load(session)
        if record is None or record.ended_at:
            return
        record.cwd, record.branch, record.last_seen = cwd, branch, _stamp()
        _write(record)


def end(session: str, *, timeout: float = lock.TIMEOUT_SECONDS) -> None:
    with _locked(timeout):
        record = load(session)
        if record is None or record.ended_at:
            return
        record.ended_at = _stamp()
        _write(record)


def set_name(session: str, name: str) -> Record:
    """Rename `session`. Raises NameTaken when a live session holds the name,
    KeyError when `session` was never registered."""
    name = name.strip()
    if not name:
        raise ValueError("a session name cannot be empty")
    with _locked():
        record = load(session)
        if record is None:
            raise KeyError(session)
        holder = _live_holder(name, besides=session)
        if holder is not None:
            raise NameTaken(holder)
        record.name = name
        _write(record)
        return record


def adopt_display_name(
    session: str, display_name: str, *, timeout: float = lock.TIMEOUT_SECONDS
) -> Record | None:
    """Give `session` the harness's display name when it still has the fallback
    `<agent>-<short id>`: the harness may write its name only after the session
    registered. Suffixed like a default in `register`; a name set any other way
    is kept. The record when renamed, else None."""
    display_name = display_name.strip()
    if not display_name:
        return None
    with _locked(timeout):
        record = load(session)
        if record is None or not is_fallback_name(record):
            return None
        record.name = _free_name(display_name, session)
        _write(record)
        return record


def set_work_item(
    session: str,
    item_id: str,
    title: str,
    project: str | None = None,
    *,
    ticket: str | None = None,
) -> None:
    """Point the record at the work item the session claimed, and at the project
    whose ledger holds it (a session may claim in a sibling project, so its own
    project_root is not enough). No-op for an unregistered session: claiming work
    must not depend on the registry."""
    with _locked():
        record = load(session)
        if record is None:
            return
        record.work_item = {"id": item_id, "title": title}
        if project:
            record.work_item["project"] = project
        if ticket:
            record.work_item["ticket"] = ticket
        _write(record)


def clear_work_item(session: str, item_id: str) -> None:
    """Take the record off `item_id` once that item is closed, so the item no
    longer addresses the session. A record pointing at another item keeps it;
    an unregistered session is a no-op."""
    with _locked():
        record = load(session)
        if record is None or (record.work_item or {}).get("id") != item_id:
            return
        record.work_item = None
        _write(record)


# --- resolution --------------------------------------------------------------------


def _work_keys(record: Record) -> set[str]:
    """The item id and its ticket; a record written before tickets were stored
    has the id only."""
    item = record.work_item or {}
    keys = {str(item.get(field) or "").casefold() for field in ("id", "ticket")}
    keys.discard("")
    return keys


def resolve(target: str, *, sender: str | None) -> Record:
    """The one live session `target` names: exact name, else work item id or key,
    else session id or unique id prefix -- the first form that matches anything
    decides, and it must match exactly one session."""
    candidates = live()
    target = target.strip()
    if not target:
        raise NoMatch(candidates)
    folded = target.casefold()
    steps = (
        [r for r in candidates if r.name == target],
        [r for r in candidates if folded in _work_keys(r)],
        [r for r in candidates if r.id == target]
        or [r for r in candidates if r.id.startswith(target)],
    )
    for matches in steps:
        if not matches:
            continue
        if len(matches) > 1:
            raise Ambiguous(matches)
        if sender is not None and matches[0].id == sender:
            raise SelfTarget()
        return matches[0]
    raise NoMatch(candidates)
