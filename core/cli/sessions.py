"""The session commands: who is running, what each is called, and the mail between them.

Every refusal here exits 2 with a one-line reason and, where the caller has to
choose again, the sessions it can choose from. The sender of a message is the
session variable and nothing else: a command that needs to know which session
it runs in refuses when there is none, rather than guess from the directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core import mail, sessions

REFUSED = 2
_INBOX_HEADERS = 5


def _own_session() -> str | None:
    """This session's id from the harness's variable, or None after saying why."""
    from core import hooks, telemetry

    cwd = os.getcwd()
    root = hooks.project_root(cwd) or Path(cwd)
    value, _ = telemetry.detect_session(root)
    if value:
        return value
    names = " or ".join(telemetry.SESSION_VARIABLES)
    print(
        f"error: no session id: {names} is not set, so this session cannot be identified",
        file=sys.stderr,
    )
    return None


def _age(stamp: str) -> str:
    moment = sessions._parse(stamp)
    if moment is None:
        return "?"
    seconds = max(int((sessions._now() - moment).total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _project(record: sessions.Record) -> str:
    return Path(record.project_root).name if record.project_root else "-"


def _work(record: sessions.Record) -> str:
    return str((record.work_item or {}).get("id") or "-")


def _candidate_line(record: sessions.Record) -> str:
    return (
        f"  {record.name}  {record.agent}  {_project(record)}  "
        f"{record.branch or '-'}  {_work(record)}"
    )


def _refuse(reason: str, candidates: list[sessions.Record] | None = None) -> int:
    print(f"error: {reason}", file=sys.stderr)
    for record in candidates or []:
        print(_candidate_line(record), file=sys.stderr)
    return REFUSED


def _refuse_resolution(exc: Exception, target: str) -> int | None:
    if isinstance(exc, sessions.NoMatch):
        return _refuse(f"no live session matches {target!r}; live sessions:", exc.live)
    if isinstance(exc, sessions.Ambiguous):
        return _refuse(
            f"more than one live session matches {target!r}; "
            "name one by its session id",
            exc.candidates,
        )
    if isinstance(exc, sessions.SelfTarget):
        return _refuse("that is this session; a message needs another recipient")
    return None


def cmd_sessions(args: argparse.Namespace) -> int:
    records = sessions.all_records() if args.all else sessions.live()
    rows = [("NAME", "AGENT", "PROJECT", "BRANCH", "WORK", "SEEN", "UNREAD")]
    for record in sorted(records, key=lambda r: r.last_seen, reverse=True):
        rows.append(
            (
                record.name,
                record.agent,
                _project(record),
                record.branch or "-",
                _work(record),
                _age(record.last_seen),
                str(len(mail.undelivered(record.id))),
            )
        )
    if len(rows) == 1:
        print("no sessions" if args.all else "no live sessions")
        return 0
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0


def cmd_session_name(args: argparse.Namespace) -> int:
    own = _own_session()
    if own is None:
        return REFUSED
    try:
        record = sessions.set_name(own, args.name)
    except sessions.NameTaken as exc:
        return _refuse(str(exc), [exc.holder])
    except KeyError:
        return _refuse(
            "this session is not registered yet; its hooks register it on the next prompt"
        )
    except ValueError as exc:
        return _refuse(str(exc))
    print(f"{record.id}\t{record.name}")
    return 0


def cmd_session_show(args: argparse.Namespace) -> int:
    try:
        record = sessions.resolve(args.target, sender=None)
    except (sessions.NoMatch, sessions.Ambiguous) as exc:
        return _refuse_resolution(exc, args.target)
    for label, value in (
        ("name", record.name),
        ("id", record.id),
        ("agent", record.agent),
        ("host", record.host),
        ("project", record.project_root or "-"),
        ("cwd", record.cwd),
        ("branch", record.branch or "-"),
        ("work", _work(record)),
        ("started", record.started_at),
        ("last seen", f"{record.last_seen} ({_age(record.last_seen)} ago)"),
    ):
        print(f"{label}: {value}")
    waiting = {message.id for message in mail.undelivered(record.id)}
    headers = mail.inbox(record.id, include_delivered=True)[-_INBOX_HEADERS:]
    if headers:
        print("inbox:")
    for message in headers:
        state = "unread" if message.id in waiting else "read"
        print(f"  {message.id}  {state}  from {message.sender_name}  {message.sent_at}")
    return 0


def _reached_through_work_item(target: str, recipient: sessions.Record) -> bool:
    target = target.strip()
    return target != recipient.name and target.casefold() in sessions._work_keys(recipient)


def _log_on_work_item(
    sender: str, message: mail.Message, recipient: sessions.Record
) -> None:
    """Leave the message on the recipient's work item in its project ledger, so the
    durable part of the exchange outlives both sessions. The message is already in
    the inbox: whatever goes wrong here is said on stderr and changes nothing else."""
    from core import work

    item = recipient.work_item or {}
    item_id = item.get("id")
    project = item.get("project") or recipient.project_root
    if not item_id:
        print(f"note: {recipient.name} has no work item; nothing logged", file=sys.stderr)
        return
    if not project:
        print(
            f"note: {recipient.name} has no project; nothing logged on {item_id}",
            file=sys.stderr,
        )
        return
    mine = sessions.load(sender)
    try:
        work.append(
            project,
            work.EVENT_LOG,
            item_id,
            session=sender,
            agent=mine.agent if mine else None,
            body=f"msg from {message.sender_name}: {message.body}",
        )
    except Exception as exc:
        print(f"note: could not log on {item_id}: {exc}", file=sys.stderr)


def cmd_msg_send(args: argparse.Namespace) -> int:
    own = _own_session()
    if own is None:
        return REFUSED
    try:
        message, recipient = mail.send(
            own, args.to, args.body, file=args.file, reply_to=args.reply_to
        )
    except (sessions.NoMatch, sessions.Ambiguous, sessions.SelfTarget) as exc:
        return _refuse_resolution(exc, args.to)
    except FileNotFoundError as exc:
        return _refuse(str(exc))
    print(
        f"{message.id}\tsent to {recipient.name} ({recipient.agent}, {_project(recipient)})"
    )
    if _reached_through_work_item(args.to, recipient) or args.log:
        _log_on_work_item(own, message, recipient)
    return 0


def cmd_msg_inbox(args: argparse.Namespace) -> int:
    own = _own_session()
    if own is None:
        return REFUSED
    unread = mail.undelivered(own)
    waiting = {message.id for message in unread}
    shown = mail.inbox(own, include_delivered=True) if args.all else unread
    shown = sorted(shown, key=lambda m: m.id not in waiting)
    if not shown:
        print("no messages" if args.all else "no unread messages")
        return 0
    print(mail.FRAME)
    for message in shown:
        state = "unread" if message.id in waiting else "read"
        print(
            f"[{message.id} {state} from {message.sender_name} {message.sent_at}] "
            f"{mail.indented(message.body)}"
        )
        if message.attachment:
            print(f"  file: {message.attachment}")
        if message.reply_to:
            print(f"  in reply to: {message.reply_to}")
    # Showing a message here is delivering it: the hook must not inject it again.
    mail.mark_delivered(own, [message.id for message in unread if message in shown])
    return 0


def cmd_msg_reply(args: argparse.Namespace) -> int:
    own = _own_session()
    if own is None:
        return REFUSED
    try:
        message, recipient = mail.reply(own, args.id, args.body)
    except KeyError:
        return _refuse(f"no message {args.id!r} in this session's inbox")
    except (sessions.NoMatch, sessions.Ambiguous, sessions.SelfTarget) as exc:
        return _refuse_resolution(exc, args.id)
    print(
        f"{message.id}\tsent to {recipient.name} ({recipient.agent}, {_project(recipient)})"
    )
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    return {"name": cmd_session_name, "show": cmd_session_show}[args.session_command](
        args
    )


def cmd_msg(args: argparse.Namespace) -> int:
    return {"send": cmd_msg_send, "inbox": cmd_msg_inbox, "reply": cmd_msg_reply}[
        args.msg_command
    ](args)
