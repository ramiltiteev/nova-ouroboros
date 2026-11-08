"""Transactional editorial queue. All persistent writes stay in host skill state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlunsplit

STATUSES = {"found": "найден", "reviewing": "на разборе", "ready": "готов", "rejected": "отклонён"}
ALIASES = {**{k: k for k in STATUSES}, **{v: k for k, v in STATUSES.items()}, "отклонен": "rejected"}

SCHEMA_VERSION = 2
KINDS = {"idea": "идея", "material": "материал", "draft": "черновик", "conclusion": "вывод", "question": "вопрос"}
KIND_ALIASES = {**{k: k for k in KINDS}, **{v: k for k, v in KINDS.items()}}
QUESTION_STATES = {"open": "открыт", "answered": "отвечен", "dropped": "снят"}
QUESTION_ALIASES = {**{k: k for k in QUESTION_STATES}, **{v: k for k, v in QUESTION_STATES.items()}}
LINK_TYPES = ("continuation", "correction", "related", "evidence", "source", "answers")
# One argv string must stay below Linux MAX_ARG_STRLEN (128 KiB including NUL).
TEXT_BYTES = 120000
PAGE_BYTES = 120000
RECORD_TEXT_BYTES = 60000
CHUNK_BYTES = 100000
PREVIEW_CHARS = 280
MAX_REFS = 200

# Version 1 DDL, kept byte-for-byte as 0.1.0 created it.
SCHEMA_V1 = (
    """CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL, url_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, summary TEXT NOT NULL, interest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('found','reviewing','ready','rejected')),
            rejection_reason TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
    """CREATE INDEX IF NOT EXISTS cards_status_id ON cards(status, id)""",
    """CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id),
            status TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL
        )""",
)

# Version 2 only adds tables. Plain CREATE makes a foreign same-named table abort the migration.
SCHEMA_V2 = (
    """CREATE TABLE topics (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE records (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('idea','material','draft','conclusion','question')),
        title TEXT NOT NULL, topic_id INTEGER REFERENCES topics(id),
        dated TEXT CHECK(dated IS NULL OR dated GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        state TEXT CHECK(state IS NULL OR state IN ('open','answered','dropped')),
        state_note TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL CHECK(revision >= 1),
        search_text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((kind IN ('conclusion','question')) = (dated IS NOT NULL)),
        CHECK((kind = 'question') = (state IS NOT NULL))
    )""",
    "CREATE INDEX records_kind_id ON records(kind, id)",
    "CREATE INDEX records_topic_id ON records(topic_id, id)",
    """CREATE TABLE revisions (
        record_id INTEGER NOT NULL REFERENCES records(id), number INTEGER NOT NULL CHECK(number >= 1),
        text TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        PRIMARY KEY(record_id, number)
    )""",
    """CREATE TRIGGER revisions_contiguous BEFORE INSERT ON revisions
        WHEN NEW.number <> (SELECT coalesce(max(number), 0) + 1 FROM revisions WHERE record_id = NEW.record_id)
        BEGIN SELECT RAISE(ABORT, 'revision numbers must be contiguous'); END""",
    """CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions
        BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END""",
    """CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions
        BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END""",
    """CREATE TABLE links (
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL CHECK(type IN ('continuation','correction','related','evidence','source','answers')),
        from_record INTEGER NOT NULL REFERENCES records(id),
        to_record INTEGER REFERENCES records(id), to_card INTEGER REFERENCES cards(id),
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        CHECK((to_record IS NULL) <> (to_card IS NULL)),
        CHECK(to_record IS NULL OR to_record <> from_record)
    )""",
    "CREATE UNIQUE INDEX links_record_unique ON links(type, from_record, to_record) WHERE to_record IS NOT NULL",
    "CREATE UNIQUE INDEX links_card_unique ON links(type, from_record, to_card) WHERE to_card IS NOT NULL",
    "CREATE INDEX links_from ON links(from_record, id)",
    "CREATE INDEX links_to_record ON links(to_record, id)",
    "CREATE INDEX links_to_card ON links(to_card, id)",
)


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
    # Autocommit mode: every read and write below opens its own explicit transaction.
    db = sqlite3.connect(root / "editorial_queue.sqlite3", timeout=10, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        migrate(db)
    except BaseException:
        db.close()
        raise
    return db


def unsupported_version():
    return ValueError("Unsupported database version; preserve database and update the skill")


def migrate(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if not 0 <= version < SCHEMA_VERSION:
        raise unsupported_version()
    # The write lock serializes concurrent first openers; the version is re-read under it.
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION:
            raise unsupported_version()
        if version < SCHEMA_VERSION:
            for statement in (SCHEMA_V1 if version < 1 else ()) + SCHEMA_V2:
                db.execute(statement)
            db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Existing data violates a foreign key; database left unchanged")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:  # some SQLite errors already rolled back
            db.execute("ROLLBACK")
        raise


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


# Version 2: topics, URL-free records, immutable revisions and typed links.

def unicode_text(value, name):
    if "\x00" in value:
        raise ValueError(name + " must not contain NUL characters")
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(name + " is not valid Unicode") from None


def short_text(value, name, limit=2000, required=True):
    value = (value or "").strip()
    if required and not value:
        raise ValueError(name + " must not be blank")
    unicode_text(value, name)
    if len(value) > limit:
        raise ValueError(name + " is too long (limit " + str(limit) + " characters)")
    return value


def body_text(value):
    """Full texts are stored verbatim, including leading and trailing whitespace."""
    if not value or not value.strip():
        raise ValueError("Text must not be blank")
    size = unicode_text(value, "Text")
    if size > TEXT_BYTES:
        raise ValueError("Text is " + str(size) + " UTF-8 bytes; the limit is " + str(TEXT_BYTES))
    return value


def kind_code(value):
    try:
        return KIND_ALIASES[value.strip().casefold()]
    except KeyError:
        raise ValueError("Kind must be idea, material, draft, conclusion or question (Russian aliases accepted)") from None


def question_state(value):
    try:
        return QUESTION_ALIASES[value.strip().casefold()]
    except KeyError:
        raise ValueError("Question state must be open, answered or dropped (Russian aliases accepted)") from None


def link_type(value):
    value = value.strip().casefold()
    if value not in LINK_TYPES:
        raise ValueError("Link type must be one of: " + ", ".join(LINK_TYPES))
    return value


def calendar_date(value):
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    value = value.strip()
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Date must be a real calendar date as YYYY-MM-DD") from None


def topic_key(name):
    return " ".join(name.split()).casefold()


def page_args(args):
    if not 1 <= args.limit <= 100 or args.offset < 0:
        raise ValueError("Limit must be 1..100 and offset must be nonnegative")


def paged(key, total, offset, rows, **extra):
    page, size = [], 0
    for item in rows:
        cost = encoded_size(item) + 2
        if page and size + cost > PAGE_BYTES:
            break
        page.append(item)
        size += cost
    end = offset + len(page)
    return {"ok": True, **extra, "total": total, "offset": offset,
            "next_offset": end if end < total else None, key: page}


def text_chunk(text, start, max_chars, budget):
    """The longest piece from start whose JSON encoding fits the byte budget."""
    if not 0 <= start < len(text):
        raise ValueError("Start must be 0.." + str(len(text) - 1))
    if max_chars < 1:
        raise ValueError("Chars must be positive")
    low, high = 1, min(max_chars, len(text) - start)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(text[start:start + middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    end = start + low
    return text[start:end], (end if end < len(text) else None)


def topic_result(db, row):
    topic = dict(row)
    topic.pop("search_text", None)
    topic.pop("name_key", None)
    counts = db.execute("SELECT kind, count(*) FROM records WHERE topic_id=? GROUP BY kind", (topic["id"],)).fetchall()
    topic["records_by_kind"] = {kind: n for kind, n in counts}
    topic["record_total"] = sum(topic["records_by_kind"].values())
    return topic


def selected_topic(db, args):
    if args.id is not None:
        row = db.execute("SELECT * FROM topics WHERE id=?", (args.id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM topics WHERE name_key=?", (topic_key(args.name),)).fetchone()
    if row is None:
        raise ValueError("Topic not found")
    return row


def topic_row(db, topic_id):
    row = db.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if row is None:
        raise ValueError("Topic " + str(topic_id) + " not found")
    return row


RECORD_SELECT = """SELECT r.id, r.kind, r.title, r.topic_id, t.name AS topic, r.dated AS date, r.state,
    r.state_note, r.revision, length(v.text) AS text_chars, substr(v.text, 1, ?) AS preview,
    r.created_at, r.updated_at,
    (SELECT count(*) FROM links WHERE from_record=r.id) AS links_out_total,
    (SELECT count(*) FROM links WHERE to_record=r.id) AS links_in_total,
    (SELECT count(*) FROM links WHERE to_record=r.id AND type='correction') AS corrected_by_total
    FROM records r JOIN revisions v ON v.record_id=r.id AND v.number=r.revision
    LEFT JOIN topics t ON t.id=r.topic_id"""


def record_item(row):
    item = dict(row)
    item["kind_label"] = KINDS[item["kind"]]
    item["state_label"] = QUESTION_STATES.get(item["state"])
    return item


def record_row(db, record_id):
    row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise ValueError("Record " + str(record_id) + " not found")
    return row


def revision_text(db, record_id, number):
    row = db.execute("SELECT text FROM revisions WHERE record_id=? AND number=?", (record_id, number)).fetchone()
    if row is None:
        raise ValueError("Revision " + str(number) + " of record " + str(record_id) + " not found")
    return row[0]


def record_result(db, record_id):
    """A record with the first chunk of its current text; `text` reads the rest."""
    item = record_item(db.execute(RECORD_SELECT + " WHERE r.id=?", (PREVIEW_CHARS, record_id)).fetchone())
    del item["preview"]
    item["text"], item["text_next_start"] = text_chunk(
        revision_text(db, record_id, item["revision"]), 0, TEXT_BYTES, RECORD_TEXT_BYTES)
    return item


def record_search_text(title, text, note):
    return "\n".join((title, text, note)).casefold()


def parse_ref(value):
    value = value.strip()
    prefix, separator, rest = value.partition(":")
    prefix = prefix.casefold()
    if separator and prefix in ("record", "card") and re.fullmatch(r"[0-9]+", rest.strip()):
        return prefix, int(rest)
    if separator and prefix == "url":
        return "url", rest.strip()
    if prefix in ("http", "https"):
        return "url", value
    raise ValueError("Reference must look like record:ID, card:ID or url:URL")


def resolve_ref(db, value):
    """Returns ('record', record row) or ('card', card id)."""
    kind, target = parse_ref(value)
    if kind == "record":
        return "record", record_row(db, target)
    if kind == "card":
        row = db.execute("SELECT id FROM cards WHERE id=?", (target,)).fetchone()
    else:
        row = db.execute("SELECT id FROM cards WHERE url_key=?", (canonical_url(target),)).fetchone()
    if row is None:
        raise ValueError("Card " + str(target) + " not found")
    return "card", row[0]


def reaches(db, start, goal, kind):
    return db.execute("""WITH RECURSIVE reach(id) AS (
        SELECT ? UNION SELECT l.to_record FROM links l JOIN reach ON l.from_record=reach.id
        WHERE l.type=? AND l.to_record IS NOT NULL) SELECT 1 FROM reach WHERE id=? LIMIT 1""",
        (start, kind, goal)).fetchone() is not None


def add_link(db, kind, source, target, note, stamp):
    """Checks endpoint rules and inserts one link; an identical link is returned unchanged."""
    target_kind, target_value = target
    record = target_value if target_kind == "record" else None
    if record is not None and record["id"] == source["id"]:
        raise ValueError("A record cannot link to itself")
    if kind in ("continuation", "correction", "answers") and record is None:
        raise ValueError(kind.capitalize() + " links must point to a record")
    if kind in ("continuation", "correction") and reaches(db, record["id"], source["id"], kind):
        raise ValueError("This " + kind + " link would create a cycle")
    if kind == "correction" and record["kind"] != source["kind"]:
        raise ValueError("A correction links two records of the same kind")
    if kind in ("evidence", "answers") and source["kind"] != "conclusion":
        raise ValueError(kind.capitalize() + " links start from a conclusion")
    if kind == "evidence" and record is not None and record["kind"] == "question":
        raise ValueError("A question cannot be evidence")
    if kind == "answers" and record["kind"] != "question":
        raise ValueError("Answers links point to a question")
    if kind == "source" and record is not None and record["kind"] != "material":
        raise ValueError("A source is a card or a material record")
    from_id, to_record = source["id"], record["id"] if record is not None else None
    to_card = target_value if target_kind == "card" else None
    if kind == "related" and to_record is not None and to_record < from_id:
        from_id, to_record = to_record, from_id  # related is symmetric; store each pair once
    row = db.execute("SELECT * FROM links WHERE type=? AND from_record=? AND to_record IS ? AND to_card IS ?",
                     (kind, from_id, to_record, to_card)).fetchone()
    if row is not None:
        return dict(row), False
    cursor = db.execute("INSERT INTO links(type,from_record,to_record,to_card,note,created_at) VALUES (?,?,?,?,?,?)",
                        (kind, from_id, to_record, to_card, note, stamp))
    return dict(db.execute("SELECT * FROM links WHERE id=?", (cursor.lastrowid,)).fetchone()), True


def cmd_topic_add(db, args):
    name = short_text(args.name, "Topic name", 200)
    description = short_text(args.description, "Description", 4000, required=False)
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT * FROM topics WHERE name_key=?", (topic_key(name),)).fetchone()
    if row is not None:
        return {"ok": True, "created": False, "topic": topic_result(db, row)}
    stamp = now()
    cursor = db.execute("""INSERT INTO topics(name,name_key,description,search_text,created_at,updated_at)
        VALUES (?,?,?,?,?,?)""", (name, topic_key(name), description,
                                   (name + "\n" + description).casefold(), stamp, stamp))
    return {"ok": True, "created": True, "topic": topic_result(db, topic_row(db, cursor.lastrowid))}


def cmd_topic_get(db, args):
    db.execute("BEGIN")
    return {"ok": True, "topic": topic_result(db, selected_topic(db, args))}


def cmd_topics(db, args):
    page_args(args)
    where, params = "", []
    if args.query:
        where = " WHERE instr(t.search_text, ?) > 0"
        params.append(short_text(args.query, "Query").casefold())
    db.execute("BEGIN")
    total = db.execute("SELECT count(*) FROM topics t" + where, params).fetchone()[0]
    rows = db.execute("""SELECT t.id, t.name, t.description, t.created_at, t.updated_at,
        (SELECT count(*) FROM records WHERE topic_id=t.id) AS record_total
        FROM topics t""" + where + " ORDER BY t.id LIMIT ? OFFSET ?", params + [args.limit, args.offset]).fetchall()
    return paged("topics", total, args.offset, [dict(row) for row in rows])


def cmd_topic_set(db, args):
    db.execute("BEGIN IMMEDIATE")
    record = record_row(db, args.id)
    topic_id = None if args.clear else topic_row(db, args.topic)["id"]
    db.execute("UPDATE records SET topic_id=?, updated_at=? WHERE id=?", (topic_id, now(), record["id"]))
    return {"ok": True, "record": record_result(db, record["id"])}


def cmd_record_add(db, args):
    kind = kind_code(args.kind)
    title = short_text(args.title, "Title")
    text = body_text(args.text)
    note = short_text(args.note, "Note", required=False)
    if kind in ("conclusion", "question"):
        dated = calendar_date(args.date)
    elif args.date is not None:
        raise ValueError("Only conclusions and questions carry a date")
    else:
        dated = None
    if args.evidence and kind != "conclusion":
        raise ValueError("Only conclusions take --evidence")
    if len(args.source) + len(args.evidence) > MAX_REFS:
        raise ValueError("At most " + str(MAX_REFS) + " --source/--evidence references per addition; link the rest")
    state = "open" if kind == "question" else None
    db.execute("BEGIN IMMEDIATE")
    topic_id = topic_row(db, args.topic)["id"] if args.topic is not None else None
    stamp = now()
    cursor = db.execute("""INSERT INTO records
        (kind,title,topic_id,dated,state,revision,search_text,created_at,updated_at)
        VALUES (?,?,?,?,?,1,?,?,?)""",
        (kind, title, topic_id, dated, state, record_search_text(title, text, ""), stamp, stamp))
    record_id = cursor.lastrowid
    db.execute("INSERT INTO revisions(record_id,number,text,note,created_at) VALUES (?,1,?,?,?)",
               (record_id, text, note, stamp))
    record = record_row(db, record_id)
    # A failing reference aborts the whole addition, including the record inserted above.
    links = [add_link(db, "source", record, resolve_ref(db, ref), "", stamp)[0] for ref in args.source]
    links += [add_link(db, "evidence", record, resolve_ref(db, ref), "", stamp)[0] for ref in args.evidence]
    return {"ok": True, "created": True, "record": record_result(db, record_id), "links": links}


def cmd_record(db, args):
    db.execute("BEGIN")
    return {"ok": True, "record": record_result(db, record_row(db, args.id)["id"])}


def cmd_records(db, args):
    page_args(args)
    clauses, params = [], []
    if args.kind:
        clauses.append("r.kind=?")
        params.append(kind_code(args.kind))
    if args.topic is not None:
        clauses.append("r.topic_id=?")
        params.append(args.topic)
    if args.state:
        clauses.append("r.state=?")
        params.append(question_state(args.state))
    if args.query:
        clauses.append("instr(r.search_text, ?) > 0")
        params.append(short_text(args.query, "Query").casefold())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    db.execute("BEGIN")
    total = db.execute("SELECT count(*) FROM records r" + where, params).fetchone()[0]
    rows = db.execute(RECORD_SELECT + where + " ORDER BY r.id LIMIT ? OFFSET ?",
                      [PREVIEW_CHARS] + params + [args.limit, args.offset]).fetchall()
    return paged("records", total, args.offset, [record_item(row) for row in rows])


def cmd_revise(db, args):
    text = body_text(args.text)
    note = short_text(args.note, "Note", required=False)
    db.execute("BEGIN IMMEDIATE")
    record = record_row(db, args.id)
    if record["kind"] != "draft":
        raise ValueError("Only drafts are revised; link a correcting record instead")
    current = record["revision"]
    if args.expect is not None and args.expect != current:
        raise ValueError("Draft " + str(record["id"]) + " is at revision " + str(current) + ", not " +
                         str(args.expect) + "; read the latest revision and retry")
    if revision_text(db, record["id"], current) == text:
        return {"ok": True, "created": False, "revision": current, "record": record_result(db, record["id"])}
    stamp = now()
    db.execute("INSERT INTO revisions(record_id,number,text,note,created_at) VALUES (?,?,?,?,?)",
               (record["id"], current + 1, text, note, stamp))
    db.execute("UPDATE records SET revision=?, search_text=?, updated_at=? WHERE id=?",
               (current + 1, record_search_text(record["title"], text, record["state_note"]), stamp, record["id"]))
    return {"ok": True, "created": True, "revision": current + 1, "record": record_result(db, record["id"])}


def cmd_revisions(db, args):
    page_args(args)
    db.execute("BEGIN")
    record = record_row(db, args.id)
    rows = db.execute("""SELECT number, note, created_at, length(text) AS text_chars, substr(text, 1, ?) AS preview
        FROM revisions WHERE record_id=? ORDER BY number LIMIT ? OFFSET ?""",
                      (PREVIEW_CHARS, record["id"], args.limit, args.offset)).fetchall()
    return paged("revisions", record["revision"], args.offset, [dict(row) for row in rows], record_id=record["id"])


def cmd_text(db, args):
    db.execute("BEGIN")
    record = record_row(db, args.id)
    number = record["revision"] if args.revision is None else args.revision
    text = revision_text(db, record["id"], number)
    piece, next_start = text_chunk(text, args.start, args.chars, CHUNK_BYTES)
    return {"ok": True, "record_id": record["id"], "revision": number, "latest_revision": record["revision"],
            "total_chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "start": args.start, "next_start": next_start, "text": piece}


def cmd_question_state(db, args):
    state = question_state(args.state)
    note = short_text(args.note, "Note", required=False)
    db.execute("BEGIN IMMEDIATE")
    record = record_row(db, args.id)
    if record["kind"] != "question":
        raise ValueError("Record " + str(record["id"]) + " is not a question")
    text = revision_text(db, record["id"], record["revision"])
    db.execute("UPDATE records SET state=?, state_note=?, search_text=?, updated_at=? WHERE id=?",
               (state, note, record_search_text(record["title"], text, note), now(), record["id"]))
    return {"ok": True, "record": record_result(db, record["id"])}


def cmd_link(db, args):
    kind = link_type(args.type)
    note = short_text(args.note, "Note", required=False)
    db.execute("BEGIN IMMEDIATE")
    source = record_row(db, args.source)
    link, created = add_link(db, kind, source, resolve_ref(db, args.to), note, now())
    return {"ok": True, "created": created, "link": link}


def cmd_links(db, args):
    page_args(args)
    if args.direction not in ("out", "in", "both"):
        raise ValueError("Direction must be out, in or both")
    db.execute("BEGIN")
    if args.record is not None:
        anchor = record_row(db, args.record)["id"]
        sides = {"out": ["l.from_record=?"], "in": ["l.to_record=?"],
                 "both": ["l.from_record=?", "l.to_record=?"]}[args.direction]
    else:
        anchor = resolve_ref(db, "card:" + str(args.card) if args.card is not None else "url:" + args.url)[1]
        sides = ["l.to_card=?"]
    where = "(" + " OR ".join(sides) + ")"
    params = [anchor] * len(sides)
    if args.type:
        where += " AND l.type=?"
        params.append(link_type(args.type))
    total = db.execute("SELECT count(*) FROM links l WHERE " + where, params).fetchone()[0]
    rows = db.execute("""SELECT l.id, l.type, l.from_record, f.kind AS from_kind, f.title AS from_title,
        l.to_record, t.kind AS to_kind, l.to_card, coalesce(t.title, c.title) AS to_title, c.url AS to_card_url,
        l.note, l.created_at
        FROM links l JOIN records f ON f.id=l.from_record LEFT JOIN records t ON t.id=l.to_record
        LEFT JOIN cards c ON c.id=l.to_card WHERE """ + where + " ORDER BY l.id LIMIT ? OFFSET ?",
                      params + [args.limit, args.offset]).fetchall()
    return paged("links", total, args.offset, [dict(row) for row in rows])


COMMANDS = {
    "topic-add": cmd_topic_add, "topic-get": cmd_topic_get, "topics": cmd_topics, "topic-set": cmd_topic_set,
    "record-add": cmd_record_add, "record": cmd_record, "records": cmd_records, "revise": cmd_revise,
    "revisions": cmd_revisions, "text": cmd_text, "question-state": cmd_question_state,
    "link": cmd_link, "links": cmd_links,
}


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

    def paging(sub):
        sub.add_argument("--limit", type=int, default=20)
        sub.add_argument("--offset", type=int, default=0)

    sub = commands.add_parser("topic-add")
    sub.add_argument("--name", required=True)
    sub.add_argument("--description", default="")
    sub = commands.add_parser("topic-get")
    selector = sub.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", type=int)
    selector.add_argument("--name")
    sub = commands.add_parser("topics")
    sub.add_argument("--query", default="")
    paging(sub)
    sub = commands.add_parser("topic-set")
    sub.add_argument("--id", type=int, required=True)
    selector = sub.add_mutually_exclusive_group(required=True)
    selector.add_argument("--topic", type=int)
    selector.add_argument("--clear", action="store_true")
    sub = commands.add_parser("record-add")
    for name in ("kind", "title", "text"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--topic", type=int)
    sub.add_argument("--date")
    sub.add_argument("--note", default="")
    sub.add_argument("--source", action="append", default=[])
    sub.add_argument("--evidence", action="append", default=[])
    sub = commands.add_parser("record")
    sub.add_argument("--id", type=int, required=True)
    sub = commands.add_parser("records")
    sub.add_argument("--kind", default="")
    sub.add_argument("--topic", type=int)
    sub.add_argument("--state", default="")
    sub.add_argument("--query", default="")
    paging(sub)
    sub = commands.add_parser("revise")
    sub.add_argument("--id", type=int, required=True)
    sub.add_argument("--text", required=True)
    sub.add_argument("--note", default="")
    sub.add_argument("--expect", type=int)
    sub = commands.add_parser("revisions")
    sub.add_argument("--id", type=int, required=True)
    paging(sub)
    sub = commands.add_parser("text")
    sub.add_argument("--id", type=int, required=True)
    sub.add_argument("--revision", type=int)
    sub.add_argument("--start", type=int, default=0)
    sub.add_argument("--chars", type=int, default=TEXT_BYTES)
    sub = commands.add_parser("question-state")
    sub.add_argument("--id", type=int, required=True)
    sub.add_argument("--state", required=True)
    sub.add_argument("--note", default="")
    sub = commands.add_parser("link")
    sub.add_argument("--type", required=True)
    sub.add_argument("--from", dest="source", type=int, required=True)
    sub.add_argument("--to", required=True)
    sub.add_argument("--note", default="")
    sub = commands.add_parser("links")
    selector = sub.add_mutually_exclusive_group(required=True)
    selector.add_argument("--record", type=int)
    selector.add_argument("--card", type=int)
    selector.add_argument("--url")
    sub.add_argument("--type", default="")
    sub.add_argument("--direction", default="both")
    paging(sub)
    return cli


def main():
    args = parser().parse_args()
    db = None
    try:
        db = connect()
        with db:
            result = COMMANDS.get(args.command, execute)(db, args)
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
