import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from x_bookmarks import cli
from x_bookmarks.triage import (apply_plan, build_plan, load_rules, parse_plan, render_plan, tfidf_vectors,
                                cosine, write_plan)
from x_bookmarks.triage_web import PlanStore, host_allowed, make_handler
from http.server import ThreadingHTTPServer


def note(folder, post_id, author, text, media=None, extra=""):
    body = (f'---\nsource: "x"\nurl: "https://x.com/i/status/{post_id}"\npost_id: "{post_id}"\n'
            f'author: "{author}"\ncreated_at: "2026-09-08T09:00:00.000Z"\ntags: ["x-bookmark"]\n{extra}---\n\n'
            f'[Original post](https://x.com/i/status/{post_id})\n\n{text}\n')
    if media:
        body += f"\n![[assets/{post_id}/1.jpg]]\n"
        (folder.parent / "assets" / post_id).mkdir(parents=True, exist_ok=True)
        (folder.parent / "assets" / post_id / "1.jpg").write_bytes(b"\xff\xd8fake")
    safe = "".join(" " if c in '/\\:[]#^|' else c for c in text[:30]).strip()
    name = f"2026-09-08 - {author} - {safe} - {post_id}.md"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(body)
    return name


def tree_digest(root):
    h = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*")):
        h.update(str(path.relative_to(root)).encode())
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


class TriageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.vault = self.base / "vault"
        self.x = self.vault / "Resources" / "X"
        inbox = self.inbox = self.x / "inbox"
        note(self.x / "eink", "1", "paperfan", "My new e-ink tablet is lovely")
        note(self.x / "hobby", "2", "artist7", "sketchbook page")
        self.names = {
            "eink": note(inbox, "10", "someone", "A 60hz e-paper display driven by a tiny board"),
            "seed": note(inbox, "11", "artist7", "another sketch of the week, with some ink washes"),
            "ai": note(inbox, "12", "dev1", "How I use Claude Code subagents and MCP servers to ship faster"),
            "dev": note(inbox, "13", "dev2", "Postgres is all you need: a database for everything"),
            "print": note(inbox, "14", "maker", "Printed this planter on my Bambu, STL on printables", media=True),
            "empty": note(inbox, "15", "meme", "lol", media=True),
            "dup1": note(inbox, "16", "news", "Exactly the same announcement text appears in two posts here"),
            "dup2": note(inbox, "17", "news", "Exactly the same announcement text appears in two posts here"),
            "gone": note(inbox, "18", "ghost", "Post text unavailable. Follow the original post link to read it on X.",
                         extra='export_status: "partial"\n'),
            "other": note(inbox, "19", "rando", "Thinking about the weather and long walks in the city today"),
            "ru": note(inbox, "20", "rus", "Написал статью про платы и пайку в KiCad для датчика воздуха"),
        }
        (inbox / ".x-bookmarks" / "receipts").mkdir(parents=True)
        self.plan = self.base / "out" / "triage-plan.md"

    def actions(self, entries):
        return {e["file"]: e for e in entries}

    def test_build_plan_classifies_and_flags(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        got = self.actions(entries)
        n = self.names
        self.assertEqual(got[n["eink"]]["action"], "eink")
        self.assertEqual(got[n["seed"]]["action"], "hobby")  # author already filed in hobby/
        self.assertIn("author already in hobby/", got[n["seed"]]["why"])
        self.assertEqual(got[n["ai"]]["action"], "ai")
        self.assertEqual(got[n["dev"]]["action"], "dev")
        self.assertEqual(got[n["print"]]["action"], "3d-printing")
        self.assertEqual(got[n["ru"]]["action"], "electronics")
        self.assertEqual(got[n["other"]]["action"], "keep")
        self.assertEqual(got[n["empty"]]["action"], "delete")
        self.assertFalse(got[n["empty"]]["checked"])  # low content: proposed, not ticked
        self.assertEqual(got[n["dup2"]]["action"], "delete")
        self.assertTrue(got[n["dup2"]]["checked"])
        self.assertNotEqual(got[n["dup1"]]["action"], "delete")
        self.assertEqual(got[n["gone"]]["action"], "delete")
        self.assertEqual(sorted(context["existing"]), ["eink", "hobby"])

    def test_plan_roundtrip_preserves_entries(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        text = render_plan(entries, context)
        parsed, pctx = parse_plan(text)
        self.assertEqual(pctx["inbox_rel"], "Resources/X/inbox")
        self.assertEqual(sorted(pctx["existing"]), ["eink", "hobby"])
        self.assertEqual({(e["file"], e["action"], e["checked"]) for e in entries},
                         {(e["file"], e["action"], e["checked"]) for e in parsed})
        self.assertEqual(render_plan(parsed, pctx, generated="x"), render_plan(parse_plan(render_plan(parsed, pctx, generated="x"))[0], pctx, generated="x"))
        self.assertIn("| eink | 1 | existing |", text)

    def test_parse_rejects_bad_folder_and_missing_marker(self):
        with self.assertRaises(ValueError):
            parse_plan("# no marker\n- [x] ai ← [[a]]\n")
        bad = "<!-- x-bookmarks triage plan v1 · inbox: X/inbox -->\n- [x] ../escape ← [[a - 1]]\n"
        with self.assertRaises(ValueError):
            parse_plan(bad)
        with self.assertRaises(ValueError):
            parse_plan(bad.replace("../escape", "assets"))

    def test_spaced_folder_names_use_backticks(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        entries[0]["action"] = "3D printing"
        parsed, _ = parse_plan(render_plan(entries, context))
        self.assertEqual(parsed[0]["action"], "3D printing")

    def test_apply_on_copy_only(self):
        before = tree_digest(self.vault)
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        got = self.actions(entries)
        got[self.names["other"]].update(action="misc")
        got[self.names["dev"]]["checked"] = False
        write_plan(self.plan, entries, context)
        target = self.base / "copy"
        log, root, log_path = apply_plan(self.vault, self.plan, target)
        self.assertEqual(tree_digest(self.vault), before)
        self.assertEqual(root, target / "Resources" / "X")
        self.assertTrue((root / "eink" / self.names["eink"]).is_file())
        self.assertTrue((root / "misc" / self.names["other"]).is_file())
        self.assertTrue((root / "_delete" / self.names["dup2"]).is_file())
        self.assertTrue((root / "inbox" / self.names["dev"]).is_file())  # unticked
        self.assertTrue((root / "inbox" / self.names["empty"]).is_file())  # unticked delete candidate
        self.assertTrue((root / "assets" / "14" / "1.jpg").is_file())
        self.assertTrue((root / "inbox" / ".x-bookmarks" / "receipts").is_dir())
        self.assertTrue(log["copied_from_vault"])
        self.assertTrue(json.loads(log_path.read_text())["moved"])
        # Re-applying is idempotent: moved notes are skipped, nothing is copied again.
        log2, _, _ = apply_plan(self.vault, self.plan, target)
        self.assertFalse(log2["copied_from_vault"])
        self.assertEqual(log2["moved"], [])
        self.assertEqual(len(log2["skipped"]), len(log["moved"]))

    def test_apply_refuses_vault_targets(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        write_plan(self.plan, entries, context)
        for target in (self.vault, self.vault / "Resources" / "copy", self.base):
            with self.assertRaises(ValueError):
                apply_plan(self.vault, self.plan, target)

    def test_apply_does_not_overwrite_existing_destination(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        write_plan(self.plan, entries, context)
        target = self.base / "copy"
        root = target / "Resources" / "X"
        import shutil
        shutil.copytree(self.x, root)
        (root / ".x-bookmarks-triage-copy").write_text("{}")  # as if made by an earlier apply
        (root / "eink" / self.names["eink"]).write_text("edited copy")
        log, _, _ = apply_plan(self.vault, self.plan, target)
        self.assertEqual((root / "eink" / self.names["eink"]).read_text(), "edited copy")
        self.assertTrue((root / "inbox" / self.names["eink"]).is_file())
        self.assertIn(self.names["eink"], [s["file"] for s in log["skipped"]])

    def test_custom_rules_file(self):
        rules_path = self.base / "rules.json"
        rules_path.write_text(json.dumps({"topics": {"weather": {"strong": ["weather"]}, "eink": {"boost": 1.0}}}))
        rules = load_rules(rules_path)
        self.assertEqual(rules["eink"]["boost"], 1.0)
        entries, _ = build_plan(self.vault, "Resources/X/inbox", rules)
        self.assertEqual(self.actions(entries)[self.names["other"]]["action"], "weather")
        rules_path.write_text(json.dumps({"topics": {"../x": {"strong": ["a"]}}}))
        with self.assertRaises(ValueError):
            load_rules(rules_path)

    def test_tfidf_is_optional_and_unticked(self):
        note(self.x / "garden", "3", "g1", "tomato seedlings and compost in the raised garden beds")
        note(self.x / "garden", "4", "g2", "compost heap tips for raised garden beds this spring")
        name = note(self.inbox, "30", "g3", "my raised garden beds finally have compost and tomato seedlings")
        plain = self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[name]
        self.assertEqual(plain["action"], "keep")
        entry = self.actions(build_plan(self.vault, "Resources/X/inbox", use_tfidf=True)[0])[name]
        self.assertEqual(entry["action"], "garden")
        self.assertFalse(entry["checked"])
        a, b = tfidf_vectors(["compost garden beds", "garden beds compost", "unrelated words here"])[:2]
        self.assertGreater(cosine(a, b), 0.9)

    def test_inbox_and_plan_paths_are_validated(self):
        with self.assertRaises(ValueError):
            build_plan(self.vault, "../outside")
        args = ["x-bookmarks", "triage", "--vault", str(self.vault), "--inbox", "Resources/X/inbox",
                "--plan", str(self.inbox / "plan.md")]
        with patch.object(sys, "argv", args), self.assertRaises(SystemExit) as raised, \
                redirect_stdout(StringIO()), patch("sys.stderr", StringIO()):
            cli.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse((self.inbox / "plan.md").exists())

    def test_cli_plan_and_apply(self):
        out = StringIO()
        base = ["x-bookmarks", "triage", "--vault", str(self.vault), "--inbox", "Resources/X/inbox",
                "--plan", str(self.plan)]
        with patch.object(sys, "argv", base), redirect_stdout(out):
            cli.main()
        self.assertIn("eink", out.getvalue())
        self.assertTrue(self.plan.is_file())
        with patch.object(sys, "argv", base + ["--apply"]), self.assertRaises(SystemExit), \
                patch("sys.stderr", StringIO()):
            cli.main()
        with patch.object(sys, "argv", base + ["--apply", "--target", str(self.base / "c")]), redirect_stdout(out):
            cli.main()
        self.assertIn("Moved", out.getvalue())


class TriageQARegressionTests(unittest.TestCase):
    """Regressions for QA wave 2 findings on x-bookmark-exporter."""
    setUp = TriageTests.setUp
    actions = TriageTests.actions

    def make_plan(self):
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        write_plan(self.plan, entries, context)
        return entries

    def real_vault(self):
        # A stand-in "real" vault with the same layout and an .obsidian folder.
        import shutil
        real = self.base / "realV"
        shutil.copytree(self.vault, real)
        (real / ".obsidian").mkdir()
        return real

    def test_1_wrong_vault_with_real_vault_as_target_is_refused(self):
        self.make_plan()
        real = self.real_vault()
        before = tree_digest(real)
        with self.assertRaises(ValueError):
            apply_plan(self.vault, self.plan, real)  # --vault copyA --target realV
        self.assertEqual(tree_digest(real), before)

    def test_1_existing_unmarked_copy_is_refused(self):
        import shutil
        self.make_plan()
        target = self.base / "other"
        shutil.copytree(self.vault, target)  # a vault-like tree without .obsidian or a marker
        before = tree_digest(target)
        with self.assertRaises(ValueError) as raised:
            apply_plan(self.vault, self.plan, target)
        self.assertIn("not created by triage", str(raised.exception))
        self.assertEqual(tree_digest(target), before)

    def test_1_target_inside_an_obsidian_vault_is_refused(self):
        self.make_plan()
        real = self.real_vault()
        with self.assertRaises(ValueError):
            apply_plan(self.vault, self.plan, real / "scratch" / "copy")
        self.assertFalse((real / "scratch").exists())

    def test_2_symlinked_inbox_in_copy_is_refused(self):
        import shutil
        self.make_plan()
        real = self.base / "realV2"
        shutil.copytree(self.vault, real)
        target = self.base / "tgt"
        root = target / "Resources" / "X"
        root.mkdir(parents=True)
        (root / ".x-bookmarks-triage-copy").write_text("{}")
        (root / "inbox").symlink_to(real / "Resources" / "X" / "inbox")
        before = tree_digest(real)
        with self.assertRaises(ValueError):
            apply_plan(self.vault, self.plan, target)
        self.assertEqual(tree_digest(real), before)

    def test_2_symlinked_topic_folder_and_note_are_skipped(self):
        import shutil
        entries = self.make_plan()
        target = self.base / "tgt"
        apply_plan(self.vault, self.plan, target)  # creates a marked copy
        root = target / "Resources" / "X"
        outside = self.base / "outside"
        outside.mkdir()
        # Put the eink note back and make eink/ a symlink pointing outside the copy.
        shutil.move(root / "eink" / self.names["eink"], root / "inbox" / self.names["eink"])
        shutil.rmtree(root / "eink")
        (root / "eink").symlink_to(outside)
        # Replace the dev note with a symlink to a file outside the copy.
        (root / "inbox" / self.names["dev"]).unlink(missing_ok=True)
        shutil.move(root / "dev" / self.names["dev"], outside / "devnote.md")
        (root / "inbox" / self.names["dev"]).symlink_to(outside / "devnote.md")
        log, _, _ = apply_plan(self.vault, self.plan, target)
        reasons = {s["file"]: s["reason"] for s in log["skipped"]}
        self.assertEqual(reasons[self.names["eink"]], "unsafe path")
        self.assertEqual(reasons[self.names["dev"]], "unsafe path")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["devnote.md"])

    def test_3_platili_is_not_electronics(self):
        name = note(self.inbox, "40", "vz", "Пожалуйста откликнитесь те люди, которые когда-либо платили за подписку")
        entry = self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[name]
        self.assertNotEqual(entry["action"], "electronics")
        self.assertEqual(self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[self.names["ru"]]["action"],
                         "electronics")  # "платы" still matches

    def test_5_link_only_bookmark_is_content(self):
        name = note(self.inbox, "41", "iavins",
                    "link: https://challahscript.com/what_i_wish_someone_told_me_about_postgres")
        entry = self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[name]
        self.assertEqual(entry["action"], "dev")
        self.assertTrue(entry["checked"])
        self.assertIn("link only (challahscript.com)", entry["flags"])
        bare = note(self.inbox, "42", "nobody", "link: https://example.org/page")
        self.assertEqual(self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[bare]["action"], "keep")
        self_link = note(self.inbox, "43", "nobody2", "wow https://x.com/nobody2/status/43/video/1")
        self.assertEqual(self.actions(build_plan(self.vault, "Resources/X/inbox")[0])[self_link]["action"], "delete")

    def test_6_bracketed_filenames_roundtrip_and_apply(self):
        hand = "hand [note] about e-ink.md"
        (self.inbox / hand).write_text("A hand-made note about my e-ink reader and its e-paper screen")
        entries, context = build_plan(self.vault, "Resources/X/inbox")
        parsed, _ = parse_plan(render_plan(entries, context))
        self.assertIn(hand, [e["file"] for e in parsed])
        write_plan(self.plan, entries, context)
        log, root, _ = apply_plan(self.vault, self.plan, self.base / "c6")
        self.assertTrue((root / "eink" / hand).is_file())
        with self.assertRaises(ValueError):
            parse_plan("<!-- x-bookmarks triage plan v1 · inbox: X/inbox -->\n- [x] ai <- [[typo arrow]]\n")

    def test_7_logs_from_quick_successive_applies_do_not_collide(self):
        self.make_plan()
        target = self.base / "c7"
        fixed = __import__("datetime").datetime(2026, 10, 12, 1, 2, 3, 456789,
                                                tzinfo=__import__("datetime").timezone.utc)
        with patch("x_bookmarks.triage.datetime") as clock:
            clock.now.return_value = fixed
            _, _, first = apply_plan(self.vault, self.plan, target)
            _, _, second = apply_plan(self.vault, self.plan, target)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())
        self.assertNotEqual(json.loads(first.read_text())["moved"], json.loads(second.read_text())["moved"])


class TriageWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.vault = base / "vault"
        inbox = self.vault / "X" / "inbox"
        self.name = note(inbox, "10", "someone", "A 60hz e-paper display", media=True)
        (self.vault / "secret.md").write_text("private")
        self.plan = base / "plan.md"
        entries, context = build_plan(self.vault, "X/inbox")
        write_plan(self.plan, entries, context)
        self.store = PlanStore(self.vault, self.plan)

    def test_update_rewrites_plan(self):
        self.store.update(self.name, action="hardware")
        entries, _ = parse_plan(self.plan.read_text())
        self.assertEqual(entries[0]["action"], "hardware")
        self.assertIn("e-paper", entries[0]["detail"])  # detail survives a rewrite
        self.store.update(self.name, checked=False)
        self.assertFalse(parse_plan(self.plan.read_text())[0][0]["checked"])
        with self.assertRaises(ValueError):
            self.store.update(self.name, action="../up")
        with self.assertRaises(KeyError):
            self.store.update("nope.md", action="ai")

    def test_note_and_media_are_confined(self):
        info = self.store.note(self.name)
        self.assertEqual(info["media"], ["assets/10/1.jpg"])
        self.assertTrue(self.store.media("assets/10/1.jpg").is_file())
        for bad in ("../../secret.md", "../secret.md", "inbox/" + self.name):
            with self.assertRaises(KeyError):
                self.store.media(bad)
        with self.assertRaises(KeyError):
            self.store.note("../../secret.md")

    def test_http_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertIn("Bookmark triage", urllib.request.urlopen(base + "/").read().decode())
        state = json.loads(urllib.request.urlopen(base + "/api/plan").read())
        self.assertEqual(state["entries"][0]["file"], self.name)
        self.assertIn("eink", state["folders"])
        request = urllib.request.Request(base + "/api/update", method="POST",
                                         data=json.dumps({"file": self.name, "action": "delete"}).encode(),
                                         headers={"Content-Type": "application/json", "Origin": base})
        self.assertEqual(json.loads(urllib.request.urlopen(request).read())["action"], "delete")
        self.assertEqual(parse_plan(self.plan.read_text())[0][0]["action"], "delete")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(base + "/media?path=../secret.md")
        self.assertEqual(raised.exception.code, 404)

    def post(self, base, body, headers=None):
        request = urllib.request.Request(base + "/api/update", method="POST", data=body,
                                         headers=headers or {"Content-Type": "application/json", "Origin": base})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_write_endpoint_guards(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        before = self.plan.read_text()
        body = json.dumps({"file": self.name, "action": "ai"}).encode()
        cases = [
            ({"Content-Type": "text/plain", "Origin": base}, 415),
            ({"Content-Type": "application/x-www-form-urlencoded", "Origin": base}, 415),
            ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
            ({"Content-Type": "application/json"}, 403),  # Origin is mandatory on writes
            ({"Content-Type": "application/json", "Origin": "null"}, 403),
            ({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port + 1}"}, 403),
            ({"Content-Type": "text/plain", "Origin": "http://evil.example"}, 403),  # QA wave 2 #4 repro
            ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "application/json", "Host": f"evil.example:{port}"}, 421),
        ]
        for headers, status in cases:
            self.assertEqual(self.post(base, body, headers)[0], status, headers)
        self.assertEqual(self.post(base, b"[1,2]")[0], 400)
        code, reply = self.post(base, b"{not json")
        self.assertEqual((code, reply), (400, {"error": "invalid JSON"}))
        self.assertEqual(self.plan.read_text(), before)
        ok = {"Content-Type": "application/json; charset=utf-8", "Origin": base, "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.post(base, body, ok), (200, {"file": self.name, "action": "ai", "checked": True}))
        # Internal errors are reported generically, without paths or tracebacks.
        with patch.object(self.store, "update", side_effect=OSError(f"disk full at {self.plan}")), \
                patch("sys.stderr", StringIO()):
            code, reply = self.post(base, body)
        self.assertEqual((code, reply), (500, {"error": "internal error"}))

    def test_default_bind_is_tailscale_ip(self):
        import inspect
        from x_bookmarks import triage_web
        self.assertEqual(inspect.signature(triage_web.serve).parameters["host"].default, "100.66.44.47")
        argv = ["x-bookmarks", "triage-ui", "--vault", str(self.vault), "--plan", str(self.plan)]
        with patch.object(sys, "argv", argv), patch.dict("os.environ", {}, clear=False), \
                patch("x_bookmarks.cli.serve") as serve:
            import os
            os.environ.pop("X_BOOKMARK_TRIAGE_HOST", None)
            cli.main()
        self.assertEqual(serve.call_args.args[2], "100.66.44.47")

    def test_host_allowed(self):
        self.assertTrue(host_allowed("100.66.44.47:8724", ("100.66.44.47", 8724)))
        self.assertTrue(host_allowed("localhost:8724", ("100.66.44.47", 8724)))
        self.assertTrue(host_allowed("[::1]:8724", ("100.66.44.47", 8724)))
        self.assertFalse(host_allowed("evil.example:8724", ("100.66.44.47", 8724)))
        self.assertFalse(host_allowed("100.66.44.47:9999", ("100.66.44.47", 8724)))
        self.assertFalse(host_allowed("100.66.44.47", ("100.66.44.47", 8724)))
        self.assertTrue(host_allowed("10.0.0.5:8724", ("0.0.0.0", 8724)))
        self.assertFalse(host_allowed("rebind.example:8724", ("0.0.0.0", 8724)))


if __name__ == "__main__":
    unittest.main()
