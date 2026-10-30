"""Transactional editorial queue. All persistent writes stay in host skill state."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

STATUSES = {"found": "найден", "reviewing": "на разборе", "ready": "готов", "rejected": "отклонён"}
ALIASES = {**{k: k for k in STATUSES}, **{v: k for k, v in STATUSES.items()}, "отклонен": "rejected"}


def canonical_url(value):
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("URL must not contain whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("Use an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not accepted")
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    if port is not None and (parts.scheme.lower(), port) not in (("http", 80), ("https", 443)):
        host += ":" + str(port)
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, parts.fragment))


def status_code(value):
    try:
        return ALIASES[value.strip().casefold()]
    except KeyError:
        raise ValueError("Status must be found, reviewing, ready or rejected (Russian aliases accepted)") from None


def required_text(value, name, limit=20000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must not be blank")
    if len(value) > limit:
        raise ValueError(name + " is too long (limit " + str(limit) + " characters)")
    return value.strip()


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    state = os.environ.get("OUROBOROS_SKILL_STATE_DIR")
    if not state:
        raise ValueError("OUROBOROS_SKILL_STATE_DIR is missing; invoke through skill_exec")
    root = Path(state).resolve()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "editorial_queue.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError("Unsupported database version; preserve database and update the skill")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL, url_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, summary TEXT NOT NULL, interest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('found','reviewing','ready','rejected')),
            rejection_reason TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cards_status_id ON cards(status, id);
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id),
            status TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL
        );
        PRAGMA user_version=1;
    """)
    return db


def search_text(card):
    return "\n".join(card[k] for k in ("url", "title", "summary", "interest", "rejection_reason")).casefold()


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def ensure_card_size(card):
    if encoded_size({k: card[k] for k in ("url", "title", "summary", "interest", "rejection_reason")}) > 60000:
        raise ValueError("Card text exceeds the 60000-byte JSON budget; shorten the editorial text")


def bounded_page(rows):
    page, size = [], 0
    for row in rows:
        item = dict(row)
        item.pop("search_text", None)
        if "url" in item:
            item["status_label"] = STATUSES[item["status"]]
        cost = encoded_size(item) + 2
        if page and size + cost > 120000:
            break
        page.append(item)
        size += cost
    return page


def card_result(db, row):
    card = dict(row)
    card.pop("search_text", None)
    card["status_label"] = STATUSES[card["status"]]
    card["history_total"] = db.execute("SELECT count(*) FROM status_history WHERE card_id=?", (card["id"],)).fetchone()[0]
    return card


def selected(db, args):
    if args.id is not None:
        row = db.execute("SELECT * FROM cards WHERE id=?", (args.id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM cards WHERE url_key=?", (canonical_url(args.url),)).fetchone()
    if row is None:
        raise ValueError("Card not found")
    return row


def execute(db, args):
    if args.command == "add":
        key = canonical_url(required_text(args.url, "URL", 8192))
        # A duplicate does not validate or replace new editorial text.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM cards WHERE url_key=?", (key,)).fetchone()
        if row is not None:
            return {"ok": True, "created": False, "card": card_result(db, row)}
        card = {"url": args.url.strip(), "title": required_text(args.title, "Title", 2000),
                "summary": required_text(args.summary, "Summary"),
                "interest": required_text(args.interest, "Interest"), "rejection_reason": ""}
        ensure_card_size(card)
        stamp = now()
        cursor = db.execute("""INSERT INTO cards
            (url,url_key,title,summary,interest,status,search_text,created_at,updated_at)
            VALUES (?,?,?,?,?,'found',?,?,?)""",
            (card["url"], key, card["title"], card["summary"], card["interest"], search_text(card), stamp, stamp))
        db.execute("INSERT INTO status_history(card_id,status,reason,changed_at) VALUES (?,'found','',?)", (cursor.lastrowid, stamp))
        row = db.execute("SELECT * FROM cards WHERE id=?", (cursor.lastrowid,)).fetchone()
        return {"ok": True, "created": True, "card": card_result(db, row)}
    if args.command == "get":
        return {"ok": True, "card": card_result(db, selected(db, args))}
    if args.command == "status":
        code = status_code(args.status)
        reason = args.reason.strip()
        if code == "rejected":
            reason = required_text(reason, "Rejection reason")
        elif len(reason) > 20000:
            raise ValueError("Reason is too long")
        db.execute("BEGIN IMMEDIATE")
        card = dict(selected(db, args))
        if code == "rejected":
            card["rejection_reason"] = reason
        ensure_card_size(card)
        if encoded_size(reason) > 30000:
            raise ValueError("Reason exceeds the 30000-byte JSON budget")
        stamp = now()
        db.execute("UPDATE cards SET status=?, rejection_reason=?, search_text=?, updated_at=? WHERE id=?",
                   (code, card["rejection_reason"], search_text(card), stamp, card["id"]))
        db.execute("INSERT INTO status_history(card_id,status,reason,changed_at) VALUES (?,?,?,?)",
                   (card["id"], code, reason, stamp))
        return {"ok": True, "card": card_result(db, db.execute("SELECT * FROM cards WHERE id=?", (card["id"],)).fetchone())}
    if not 1 <= args.limit <= 100 or args.offset < 0:
        raise ValueError("Limit must be 1..100 and offset must be nonnegative")
    if args.command == "history":
        db.execute("BEGIN")
        card = selected(db, args)
        total = db.execute("SELECT count(*) FROM status_history WHERE card_id=?", (card["id"],)).fetchone()[0]
        rows = db.execute("SELECT status,reason,changed_at FROM status_history WHERE card_id=? ORDER BY id LIMIT ? OFFSET ?",
                          (card["id"], args.limit, args.offset)).fetchall()
        items = bounded_page(rows)
        end = args.offset + len(items)
        return {"ok": True, "card_id": card["id"], "total": total, "offset": args.offset,
                "next_offset": end if end < total else None, "history": items}
    clauses, params = [], []
    if args.status:
        clauses.append("status=?")
        params.append(status_code(args.status))
    if args.command == "search":
        text = required_text(args.query, "Query", 2000)
        try:
            key = canonical_url(text)
        except ValueError:
            key = ""
        clauses.append("(instr(search_text, ?) > 0 OR url_key=?)")
        params.extend((text.casefold(), key))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    # One read transaction keeps count and page consistent with concurrent writers.
    db.execute("BEGIN")
    total = db.execute("SELECT count(*) FROM cards" + where, params).fetchone()[0]
    rows = db.execute("SELECT * FROM cards" + where + " ORDER BY id LIMIT ? OFFSET ?", params + [args.limit, args.offset]).fetchall()
    items = bounded_page(rows)
    end = args.offset + len(items)
    return {"ok": True, "total": total, "offset": args.offset,
            "next_offset": end if end < total else None, "cards": items}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    for name in ("url", "title", "summary", "interest"):
        add.add_argument("--" + name, required=True)
    for name in ("get", "status", "history"):
        sub = commands.add_parser(name)
        selector = sub.add_mutually_exclusive_group(required=True)
        selector.add_argument("--id", type=int)
        selector.add_argument("--url")
        if name == "history":
            sub.add_argument("--limit", type=int, default=20)
            sub.add_argument("--offset", type=int, default=0)
        if name == "status":
            sub.add_argument("--status", required=True)
            sub.add_argument("--reason", default="")
    for name in ("search", "list"):
        sub = commands.add_parser(name)
        if name == "search":
            sub.add_argument("--query", required=True)
        sub.add_argument("--status", default="")
        sub.add_argument("--limit", type=int, default=20)
        sub.add_argument("--offset", type=int, default=0)
    return cli


def main():
    args = parser().parse_args()
    db = None
    try:
        db = connect()
        with db:
            result = execute(db, args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
