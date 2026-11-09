"""Black-box tests: every case runs scripts/queue.py in fresh processes against private temporary state.

Run from the payload root: python3 -B -m unittest discover -s tests
The installed queue database is never opened; OUROBOROS_SKILL_STATE_DIR is replaced per test.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "queue.py"
# Byte-identical copy of scripts/queue.py from version 0.1.0, used only to build legacy databases.
LEGACY = HERE / "legacy_queue_v0_1_0.py"
STDOUT_CAP = 256 * 1024
V1_OBJECTS = ("cards", "cards_status_id", "status_history")


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="editorial-queue-test-")
        self.state = Path(self._tmp.name) / "state"
        inherited = os.environ.get("OUROBOROS_SKILL_STATE_DIR")
        assert inherited is None or Path(inherited).resolve() != self.state.resolve()
        self.db_path = self.state / "editorial_queue.sqlite3"
        self.env = {k: v for k, v in os.environ.items() if k != "OUROBOROS_SKILL_STATE_DIR"}
        self.env.update(OUROBOROS_SKILL_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        try:
            if self.db_path.exists():
                with self.inspect() as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            self._tmp.cleanup()

    def inspect(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA foreign_keys=ON")
        return closing(db)

    def command(self, args, script=SCRIPT):
        return [sys.executable, "-B", str(script), *[str(a) for a in args]]

    def run_raw(self, *args, script=SCRIPT, env=None):
        proc = subprocess.run(self.command(args, script), env=env or self.env, capture_output=True, timeout=60)
        self.assertLessEqual(len(proc.stdout), STDOUT_CAP)
        return proc

    def call(self, *args, script=SCRIPT):
        proc = self.run_raw(*args, script=script)
        result = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(proc.returncode, 0, result)
        self.assertTrue(result["ok"], result)
        return result

    def fail_call(self, *args, script=SCRIPT, contains=""):
        proc = self.run_raw(*args, script=script)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn(contains, result["error"])
        return result["error"]

    def parallel(self, argv_list):
        """Starts every process before waiting for any, so writers really overlap."""
        procs = [subprocess.Popen(self.command(a), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for a in argv_list]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertIn(proc.returncode, (0, 1), err)
            results.append(json.loads(out.decode("utf-8")))
        return results

    def dump(self, tables=("cards", "status_history")):
        with self.inspect() as db:
            schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name IN (?,?,?) ORDER BY name",
                                V1_OBJECTS).fetchall()
            rows = {t: db.execute("SELECT * FROM " + t + " ORDER BY id").fetchall() for t in tables}
            return schema, rows

    def full_dump(self):
        with self.inspect() as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: db.execute("SELECT * FROM " + n).fetchall() for n in names}

    def user_version(self):
        with self.inspect() as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    def add(self, kind, title, text, *extra):
        return self.call("record-add", "--kind", kind, "--title", title, "--text", text, *extra)["record"]

    def read_all(self, command, key, *args, limit=100):
        items, offset, pages = [], 0, 0
        while offset is not None:
            page = self.call(command, *args, "--limit", limit, "--offset", offset)
            self.assertLessEqual(len(json.dumps(page[key], ensure_ascii=False).encode()), 120000 + 4096)
            self.assertTrue(page[key])
            items += page[key]
            offset, pages = page["next_offset"], pages + 1
        self.assertEqual(len(items), page["total"])
        return items, pages


class closing:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.close()


def build_legacy(test):
    """A realistic 0.1.0 queue created only through the legacy CLI."""
    legacy = lambda *a: test.call(*a, script=LEGACY)
    legacy("add", "--url", "HTTPS://Example.COM:443?b=2&a=1#Frag", "--title", "Ёжик и «кавычки» 🦔",
           "--summary", "Сводка\nв две строки", "--interest", "Почему важно")
    legacy("add", "--url", "http://example.org/Path", "--title", "Second", "--summary", "S2", "--interest", "I2")
    legacy("add", "--url", "https://example.net/x", "--title", "Third", "--summary", "S3", "--interest", "I3")
    legacy("status", "--id", 1, "--status", "на разборе", "--reason", "смотрим")
    legacy("status", "--id", 2, "--status", "rejected", "--reason", "Дубль темы")
    legacy("status", "--id", 2, "--status", "found")
    legacy("status", "--url", "https://example.net/x", "--status", "готов")


LEGACY_READS = (
    ("get", "--id", 1), ("get", "--url", "https://example.com/?b=2&a=1#Frag"), ("get", "--id", 2),
    ("list",), ("list", "--status", "found"), ("list", "--limit", 1, "--offset", 1),
    ("search", "--query", "ЁЖИК"), ("search", "--query", "дубль"), ("search", "--query", "HTTP://EXAMPLE.org:80/Path"),
    ("history", "--id", 2), ("history", "--url", "https://example.net/x", "--limit", 1),
    ("add", "--url", "https://example.com/?b=2&a=1#Frag", "--title", "x", "--summary", "y", "--interest", "z"),
)


class MigrationTest(QueueTest):
    def test_legacy_database_preserved_exactly(self):
        build_legacy(self)
        before_outputs = [self.call(*a, script=LEGACY) for a in LEGACY_READS]
        before = self.dump()
        self.assertEqual(self.user_version(), 1)
        after_outputs = [self.call(*a) for a in LEGACY_READS]
        self.assertEqual(after_outputs, before_outputs)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.user_version(), 2)
        # Legacy writes keep their behaviour and IDs continue after migration.
        self.assertEqual(self.call("add", "--url", "https://new.example/", "--title", "N", "--summary", "S",
                                   "--interest", "I")["card"]["id"], 4)
        card = self.call("status", "--id", 1, "--status", "отклонен", "--reason", "Нет")["card"]
        self.assertEqual((card["status"], card["rejection_reason"], card["history_total"]), ("rejected", "Нет", 3))
        self.fail_call("status", "--id", 1, "--status", "rejected", "--reason", "  ", contains="Rejection reason")
        # A migrated card can be attached as a source of a new record.
        record = self.add("idea", "Из карточки", "текст", "--source", "url:https://EXAMPLE.com/?b=2&a=1#Frag")
        self.assertEqual(self.call("links", "--card", 1)["links"][0]["from_record"], record["id"])

    def test_fresh_v1_schema_matches_legacy(self):
        self.call("list", script=LEGACY)
        legacy_schema = self.dump()[0]
        self.db_path.unlink()
        self.call("list")
        self.assertEqual(self.dump()[0], legacy_schema)
        self.assertEqual(self.user_version(), 2)

    def test_legacy_script_refuses_migrated_database(self):
        build_legacy(self)
        self.call("list")
        self.fail_call("list", script=LEGACY, contains="Unsupported database version")

    def test_future_version_refused_without_changes(self):
        self.call("list")
        with self.inspect() as db:
            db.execute("PRAGMA user_version=3")
            db.commit()
        digest = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        for args in (("list",), ("records",), ("topic-add", "--name", "x"),
                     ("add", "--url", "https://a.example/", "--title", "t", "--summary", "s", "--interest", "i")):
            self.fail_call(*args, contains="Unsupported database version")
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), digest)

    def test_failed_migration_rolls_back(self):
        build_legacy(self)
        with self.inspect() as db:
            db.execute("CREATE TABLE records (foreign_column TEXT)")
            db.commit()
        before = self.full_dump()
        self.fail_call("list", contains="records")
        self.assertEqual(self.user_version(), 1)
        self.assertEqual(self.full_dump(), before)
        self.call("list", script=LEGACY)

    def test_foreign_key_violation_blocks_migration(self):
        build_legacy(self)
        db = sqlite3.connect(self.db_path)  # foreign keys off, as a damaged legacy file might be
        db.execute("INSERT INTO status_history(card_id,status,reason,changed_at) VALUES (999,'found','','t')")
        db.commit()
        db.close()
        digest = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.fail_call("records", contains="foreign key")
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), digest)
        with self.inspect() as db:  # repair so tearDown's integrity check describes this test's own effects
            db.execute("DELETE FROM status_history WHERE card_id=999")
            db.commit()

    def test_concurrent_first_open_migrates_once(self):
        build_legacy(self)
        before = self.dump()
        # Holding the write lock lets every opener read version 1 and queue behind the same migration lock.
        blocker = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        timer = threading.Timer(2.0, blocker.execute, ("COMMIT",))
        timer.start()
        try:
            results = self.parallel([("records",), ("list",), ("topics",), ("get", "--id", 2)] * 2 +
                                    [("topic-add", "--name", "Гонка")] * 6)
        finally:
            timer.join()
            blocker.close()
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(sum(r.get("created") is True for r in results), 1)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.user_version(), 2)

    def test_missing_state_is_an_error(self):
        env = {k: v for k, v in self.env.items() if k != "OUROBOROS_SKILL_STATE_DIR"}
        proc = self.run_raw("records", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("OUROBOROS_SKILL_STATE_DIR", json.loads(proc.stdout)["error"])


class RecordTest(QueueTest):
    def test_topics_and_url_free_ideas_materials(self):
        topic = self.call("topic-add", "--name", " ИИ   Агенты ", "--description", "Про агентов")
        self.assertTrue(topic["created"])
        again = self.call("topic-add", "--name", "ии агенты", "--description", "другое")
        self.assertEqual((again["created"], again["topic"]["description"]), (False, "Про агентов"))
        other = self.call("topic-add", "--name", "Регулирование")["topic"]["id"]
        tid = topic["topic"]["id"]
        idea = self.add("идея", "Идея без ссылки", "Мысль", "--topic", tid)
        material = self.add("material", "Заметки с конференции", "Конспект", "--topic", tid, "--note", "первая версия")
        self.assertEqual((idea["kind"], idea["kind_label"], idea["topic"]), ("idea", "идея", "ИИ   Агенты"))
        self.assertNotIn("url", idea)
        got = self.call("topic-get", "--name", "ИИ агенты")["topic"]
        self.assertEqual((got["record_total"], got["records_by_kind"]), (2, {"idea": 1, "material": 1}))
        self.assertEqual([t["id"] for t in self.call("topics", "--query", "агент")["topics"]], [tid])
        self.assertEqual(self.call("records", "--kind", "материал")["records"][0]["id"], material["id"])
        self.assertEqual(self.call("records", "--topic", tid)["total"], 2)
        self.assertEqual(self.call("records", "--query", "КОНСПЕКТ")["records"][0]["id"], material["id"])
        self.assertEqual(self.call("revisions", "--id", material["id"])["revisions"][0]["note"], "первая версия")
        self.assertEqual(self.call("topic-set", "--id", idea["id"], "--topic", other)["record"]["topic_id"], other)
        self.assertIsNone(self.call("topic-set", "--id", idea["id"], "--clear")["record"]["topic_id"])
        self.assertEqual(self.call("topic-get", "--id", tid)["topic"]["record_total"], 1)
        self.fail_call("topic-get", "--name", "нет такой", contains="Topic not found")

    def test_draft_revisions_are_numbered_and_immutable(self):
        draft = self.add("draft", "Черновик", "v1\n")
        rid = draft["id"]
        self.assertEqual(draft["revision"], 1)
        self.assertEqual(self.call("revise", "--id", rid, "--text", "v2\n", "--expect", 1, "--note", "правка")["revision"], 2)
        self.fail_call("revise", "--id", rid, "--text", "stale", "--expect", 1, contains="is at revision 2, not 1")
        retry = self.call("revise", "--id", rid, "--text", "v2\n")
        self.assertEqual((retry["created"], retry["revision"]), (False, 2))
        self.assertEqual(self.call("revise", "--id", rid, "--text", "v3")["revision"], 3)
        self.assertEqual([self.call("text", "--id", rid, "--revision", n)["text"] for n in (1, 2, 3)], ["v1\n", "v2\n", "v3"])
        latest = self.call("text", "--id", rid)
        self.assertEqual((latest["revision"], latest["latest_revision"], latest["text"]), (3, 3, "v3"))
        revisions = self.call("revisions", "--id", rid)
        self.assertEqual([r["number"] for r in revisions["revisions"]], [1, 2, 3])
        self.assertEqual(revisions["revisions"][1]["note"], "правка")
        self.fail_call("text", "--id", rid, "--revision", 4, contains="Revision 4")
        conclusion = self.add("conclusion", "Вывод", "текст")
        self.fail_call("revise", "--id", conclusion["id"], "--text", "иначе", contains="Only drafts")
        with self.inspect() as db:
            for sql in ("UPDATE revisions SET text='x' WHERE record_id=%d" % rid,
                        "DELETE FROM revisions WHERE record_id=%d" % rid,
                        "INSERT INTO revisions(record_id,number,text,note,created_at) VALUES (%d,9,'x','','t')" % rid):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            db.rollback()

    def test_dated_conclusions_and_open_questions(self):
        self.call("add", "--url", "https://source.example/a", "--title", "Источник", "--summary", "s", "--interest", "i")
        material = self.add("material", "Материал", "данные")
        old = self.add("conclusion", "Старый вывод", "Рынок растёт", "--date", "2026-10-01",
                       "--evidence", "card:1", "--evidence", "record:%d" % material["id"],
                       "--evidence", "https://SOURCE.example:443/a")
        self.assertEqual((old["date"], old["links_out_total"]), ("2026-10-01", 2))
        today = datetime.now(timezone.utc).date().isoformat()
        new = self.add("вывод", "Исправленный вывод", "Рынок стагнирует", "--evidence", "card:1")
        self.assertIn(new["date"], (today, datetime.now(timezone.utc).date().isoformat()))
        self.call("link", "--type", "correction", "--from", new["id"], "--to", "record:%d" % old["id"], "--note", "новые данные")
        self.assertEqual(self.call("record", "--id", old["id"])["record"]["corrected_by_total"], 1)
        question = self.add("вопрос", "Кто платит?", "Открытый вопрос", "--date", "2026-11-01")
        self.assertEqual((question["state"], question["state_label"], question["date"]), ("open", "открыт", "2026-11-01"))
        self.call("link", "--type", "answers", "--from", new["id"], "--to", "record:%d" % question["id"])
        answered = self.call("question-state", "--id", question["id"], "--state", "отвечен", "--note", "см. вывод")["record"]
        self.assertEqual((answered["state"], answered["state_note"]), ("answered", "см. вывод"))
        self.assertEqual(self.call("records", "--state", "answered")["records"][0]["id"], question["id"])
        self.assertEqual(self.call("records", "--state", "open")["total"], 0)
        self.assertEqual(self.call("records", "--query", "СМ. ВЫВОД")["total"], 1)
        self.call("question-state", "--id", question["id"], "--state", "open")
        self.fail_call("question-state", "--id", old["id"], "--state", "open", contains="not a question")
        self.fail_call("question-state", "--id", question["id"], "--state", "maybe", contains="Question state")
        card_links = self.call("links", "--url", "https://source.example/a")["links"]
        self.assertEqual({(l["type"], l["from_record"]) for l in card_links}, {("evidence", old["id"]), ("evidence", new["id"])})

    def test_link_endpoint_rules(self):
        self.call("add", "--url", "https://c.example/", "--title", "Card", "--summary", "s", "--interest", "i")
        idea = self.add("idea", "A", "a")["id"]
        draft = self.add("draft", "B", "b")["id"]
        draft2 = self.add("draft", "C", "c")["id"]
        conclusion = self.add("conclusion", "D", "d")["id"]
        question = self.add("question", "E", "e")["id"]
        material = self.add("material", "F", "f")["id"]
        link = lambda kind, a, ref: self.call("link", "--type", kind, "--from", a, "--to", ref)
        bad = lambda kind, a, ref, msg: self.fail_call("link", "--type", kind, "--from", a, "--to", ref, contains=msg)
        self.assertTrue(link("continuation", draft, "record:%d" % idea)["created"])
        link("continuation", draft2, "record:%d" % draft)
        bad("continuation", idea, "record:%d" % draft2, "cycle")
        bad("correction", draft, "record:%d" % idea, "same kind")
        self.assertTrue(link("correction", draft2, "record:%d" % draft)["created"])
        bad("correction", draft, "record:%d" % draft2, "cycle")
        bad("related", idea, "record:%d" % idea, "itself")
        bad("continuation", draft, "card:1", "must point to a record")
        bad("evidence", idea, "card:1", "start from a conclusion")
        bad("evidence", conclusion, "record:%d" % question, "question cannot be evidence")
        bad("answers", conclusion, "record:%d" % idea, "point to a question")
        bad("source", idea, "record:%d" % draft, "card or a material")
        self.assertTrue(link("source", idea, "record:%d" % material)["created"])
        self.assertTrue(link("source", idea, "card:1")["created"])
        bad("related", idea, "record:999", "Record 999 not found")
        bad("related", idea, "card:999", "Card 999 not found")
        bad("related", idea, "url:https://missing.example/", "not found")
        bad("related", idea, "idea:1", "Reference must look like")
        bad("parent", idea, "card:1", "Link type")
        self.fail_call("link", "--type", "related", "--from", 999, "--to", "card:1", contains="Record 999")
        first = link("related", draft, "record:%d" % idea)
        mirrored = link("related", idea, "record:%d" % draft)
        self.assertEqual((first["created"], mirrored["created"], mirrored["link"]["id"]), (True, False, first["link"]["id"]))
        self.assertEqual((first["link"]["from_record"], first["link"]["to_record"]), (idea, draft))
        self.assertFalse(link("source", idea, "card:1")["created"])
        out = self.call("links", "--record", idea, "--direction", "out")["links"]
        self.assertEqual({(l["type"], l["to_record"], l["to_card"]) for l in out},
                         {("source", material, None), ("source", None, 1), ("related", draft, None)})
        incoming = self.call("links", "--record", idea, "--direction", "in", "--type", "continuation")["links"]
        self.assertEqual([(l["from_record"], l["from_title"]) for l in incoming], [(draft, "B")])
        self.fail_call("links", "--record", idea, "--direction", "sideways", contains="Direction")


class ValidationTest(QueueTest):
    def test_invalid_writes_change_nothing(self):
        self.call("add", "--url", "https://c.example/", "--title", "Card", "--summary", "s", "--interest", "i")
        self.call("topic-add", "--name", "T")
        idea = self.add("idea", "Idea", "text")["id"]
        draft = self.add("draft", "Draft", "text")["id"]
        before = self.full_dump()
        big = "я" * 60001  # 120002 UTF-8 bytes
        failures = [
            (("record-add", "--kind", "conclusion", "--title", "x", "--text", "y",
              "--evidence", "card:1", "--evidence", "record:999"), "Record 999"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", "y",
              "--source", "card:1", "--source", "record:%d" % idea), "card or a material"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", "y", "--topic", 99), "Topic 99"),
            (("record-add", "--kind", "note", "--title", "x", "--text", "y"), "Kind must be"),
            (("record-add", "--kind", "idea", "--title", "  ", "--text", "y"), "Title must not be blank"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", " \n "), "Text must not be blank"),
            (("record-add", "--kind", "idea", "--title", "x" * 2001, "--text", "y"), "Title is too long"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", big), "120002 UTF-8 bytes"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", "y", "--date", "2026-01-01"), "Only conclusions"),
            (("record-add", "--kind", "idea", "--title", "x", "--text", "y", "--evidence", "card:1"), "Only conclusions"),
            (("record-add", "--kind", "question", "--title", "x", "--text", "y", "--date", "2026-02-30"), "calendar date"),
            (("record-add", "--kind", "question", "--title", "x", "--text", "y", "--date", "2026-2-3"), "calendar date"),
            (("record-add", "--kind", "question", "--title", "x", "--text", "y", "--date", "２０２６-01-01"), "calendar date"),
            (("revise", "--id", draft, "--text", big), "UTF-8 bytes"),
            (("revise", "--id", 999, "--text", "x"), "Record 999"),
            (("topic-set", "--id", idea, "--topic", 99), "Topic 99"),
            (("topic-add", "--name", " "), "Topic name"),
            (("records", "--limit", 0), "Limit must be"),
            (("links", "--record", idea, "--offset", -1), "Limit must be"),
            (("text", "--id", draft, "--start", 4), "Start must be"),
            (("text", "--id", draft, "--chars", 0), "Chars must be"),
            (("record", "--id", 999), "Record 999"),
            (("record-add", "--kind", "conclusion", "--title", "x", "--text", "y") + ("--evidence", "card:1") * 201,
             "At most 200"),
        ]
        for args, message in failures:
            with self.subTest(args=args[:3]):
                self.fail_call(*args, contains=message)
        # argv that is not valid UTF-8 reaches Python as lone surrogates.
        proc = subprocess.run([os.fsencode(sys.executable), b"-B", os.fsencode(SCRIPT), b"record-add", b"--kind",
                               b"idea", b"--title", b"bad \xff", b"--text", b"y"], env=self.env, capture_output=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid Unicode", json.loads(proc.stdout)["error"])
        self.assertEqual(self.full_dump(), before)
        self.assertEqual(self.run_raw("records", "--limit", "x").returncode, 2)  # argparse usage error


class UnicodeAndPagingTest(QueueTest):
    def test_unicode_round_trip_and_search(self):
        text = "  Заголовок «ёлка» 🦔 é שלום\t\"q\" \\ \x01 \n\nконец  \n"
        record = self.add("material", "Straße Ёлка 🦔", text)
        self.assertEqual(record["text"], text)
        self.assertEqual(record["text_chars"], len(text))
        stored = self.call("text", "--id", record["id"])
        self.assertEqual(stored["sha256"], hashlib.sha256(text.encode()).hexdigest())
        for query in ("ЁЛКА", "STRASSE", "🦔", "שלום"):
            self.assertEqual(self.call("records", "--query", query)["total"], 1, query)

    def test_full_text_chunks_reassemble_exactly(self):
        # 6-byte JSON escapes, 4-byte emoji and Cyrillic make chars, UTF-8 bytes and JSON bytes all differ.
        unit = "\x01" * 7 + "🦔" * 3 + "ж" * 10 + "\n\"\\"
        text = unit * (120000 // len(unit.encode()))
        draft = self.add("draft", "Большой", text)
        self.assertLess(len(draft["text"]), len(text))
        self.assertLessEqual(len(json.dumps(draft["text"], ensure_ascii=False).encode()), 60000)
        pieces, start = [], 0
        while start is not None:
            chunk = self.call("text", "--id", draft["id"], "--start", start)
            self.assertLessEqual(len(json.dumps(chunk["text"], ensure_ascii=False).encode()), 100000)
            pieces.append(chunk["text"])
            start = chunk["next_start"]
        self.assertGreater(len(pieces), 2)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(chunk["sha256"], hashlib.sha256(text.encode()).hexdigest())
        small = self.call("text", "--id", draft["id"], "--start", 5, "--chars", 3)
        self.assertEqual((small["text"], small["next_start"]), (text[5:8], 8))

    def test_large_pagination(self):
        titles = ["Ж%03d " % i + "щ" * 1995 for i in range(150)]
        with ThreadPoolExecutor(8) as pool:
            created = list(pool.map(lambda t: self.add("idea", t, "текст " * 100), titles))
        self.assertEqual(len({r["id"] for r in created}), 150)
        records, pages = self.read_all("records", "records")
        self.assertGreater(pages, 2)  # the byte budget, not the limit, ended the pages
        self.assertEqual([r["id"] for r in records], sorted(r["id"] for r in created))
        self.assertEqual(sorted(r["title"] for r in records), titles)
        evidence = [a for r in created for a in ("--evidence", "record:%d" % r["id"])]
        conclusion = self.add("conclusion", "Итог", "много источников", *evidence)
        self.assertEqual(conclusion["links_out_total"], 150)
        links, pages = self.read_all("links", "links", "--record", conclusion["id"], "--direction", "out")
        self.assertGreater(pages, 2)
        self.assertEqual(sorted(l["to_record"] for l in links), sorted(r["id"] for r in created))
        draft = self.add("draft", "Много версий", "v0")["id"]
        for n in range(1, 25):
            self.call("revise", "--id", draft, "--text", "v%d" % n)
        revisions, pages = self.read_all("revisions", "revisions", "--id", draft, limit=7)
        self.assertEqual(([r["number"] for r in revisions], pages), (list(range(1, 26)), 4))


class ConcurrencyTest(QueueTest):
    def test_concurrent_revisions_get_unique_contiguous_numbers(self):
        draft = self.add("draft", "Гонка", "v0")["id"]
        results = self.parallel([("revise", "--id", draft, "--text", "писатель %d" % i) for i in range(12)])
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(sorted(r["revision"] for r in results), list(range(2, 14)))
        revisions = self.call("revisions", "--id", draft, "--limit", 100)["revisions"]
        self.assertEqual([r["number"] for r in revisions], list(range(1, 14)))
        self.assertEqual({r["preview"] for r in revisions[1:]}, {"писатель %d" % i for i in range(12)})

    def test_concurrent_expected_revision_admits_one_writer(self):
        draft = self.add("draft", "Гонка", "v0")["id"]
        results = self.parallel([("revise", "--id", draft, "--expect", 1, "--text", "w%d" % i) for i in range(6)])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertTrue(all("not 1" in r["error"] for r in results if not r["ok"]))
        self.assertEqual(self.call("record", "--id", draft)["record"]["revision"], 2)

    def test_concurrent_duplicates_are_created_once(self):
        a = self.add("idea", "A", "a")["id"]
        b = self.add("idea", "B", "b")["id"]
        argv = ([("topic-add", "--name", "Одна тема")] * 5 +
                [("link", "--type", "related", "--from", a, "--to", "record:%d" % b)] * 3 +
                [("link", "--type", "related", "--from", b, "--to", "record:%d" % a)] * 3 +
                [("add", "--url", "https://race.example/", "--title", "t", "--summary", "s", "--interest", "i")] * 5 +
                [("record-add", "--kind", "idea", "--title", "w%d" % i, "--text", "t") for i in range(5)])
        results = self.parallel(argv)
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual([sum(r["created"] for r in results[s]) for s in (slice(0, 5), slice(5, 11), slice(11, 16))],
                         [1, 1, 1])
        self.assertEqual(self.call("records", "--kind", "idea")["total"], 7)
        self.assertEqual(self.call("list")["total"], 1)

    def test_reopen_sees_committed_state(self):
        topic = self.call("topic-add", "--name", "Persist")["topic"]["id"]
        rid = self.add("question", "Q", "body", "--topic", topic)["id"]
        snapshot = self.call("record", "--id", rid)
        self.assertEqual(self.call("record", "--id", rid), snapshot)  # a separate process, a new connection
        self.assertEqual(self.call("topic-get", "--id", topic)["topic"]["records_by_kind"], {"question": 1})


if __name__ == "__main__":
    unittest.main()
