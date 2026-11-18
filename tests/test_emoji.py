import json
import shutil
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lazygerrit.emoji import EmojiIndex, SlackError, fetch_workspace, parse_workspace, read_cache, terminal_safe, write_cache
from lazygerrit.ui.screen import CursesCanvas, VirtualCanvas
from lazygerrit.ui.text import text_width


class EmojiIndexTest(unittest.TestCase):
    def test_slack_names_aliases_and_skin_tone(self):
        idx = EmojiIndex()
        self.assertEqual(idx.lookup("+1").text, idx.lookup("thumbsup").text)
        self.assertEqual(idx.text_for(idx.lookup("heart")), "❤️")
        self.assertIsNone(idx.lookup("no_such_emoji"))
        toned = EmojiIndex(skin_tone=3)
        self.assertEqual(toned.text_for(toned.lookup("+1")), "👍🏼")
        self.assertEqual(toned.text_for(toned.lookup("female-technologist")), "👩🏼‍💻")
        self.assertEqual(toned.text_for(toned.lookup("heart")), "❤️")  # takes no skin tone
        self.assertEqual(EmojiIndex(skin_tone=9).problems, ["emoji_skin_tone must be 1 (none) to 6, not 9"])

    def test_completion_puts_exact_matches_then_favourites_first(self):
        idx = EmojiIndex()
        self.assertEqual(
            [e.name for e in idx.complete("che")[:3]], ["white_check_mark", "heavy_check_mark", "ballot_box_with_check"]
        )
        self.assertEqual([e.name for e in idx.complete("+1")], ["+1"])
        self.assertEqual(sum(e.text == "👍" for e in idx.complete("thumbs")), 1)  # +1 and thumbsup are one emoji

    def test_workspace_and_config_emoji(self):
        workspace = {"party-parrot": "https://emoji.slack-edge.com/T1/party-parrot/1.gif", "yes": "alias:white_check_mark"}
        workspace.update({"ping": "alias:pong", "pong": "alias:ping"})
        idx = EmojiIndex({"lgtm": "alias:yes", "ship": "🚢 ship it", "bad": 3}, workspace)
        self.assertEqual(idx.text_for(idx.lookup("party-parrot")), ":party-parrot:")  # an image: only its name can be posted
        self.assertEqual((idx.text_for(idx.lookup("yes")), idx.text_for(idx.lookup("lgtm"))), ("✅", "✅"))
        self.assertEqual(idx.text_for(idx.lookup("ship")), "🚢 ship it")
        self.assertIsNone(idx.lookup("ping"))
        self.assertIn("party-parrot", [e.name for e in idx.complete("parrot")])
        self.assertEqual(idx.problems, ["custom_emoji.bad must be text (or alias:name), not 3"])

    def test_shortcodes_typed_and_written(self):
        idx = EmojiIndex()
        self.assertEqual(idx.typed("Nice :tada:"), (5, "🎉"))
        self.assertEqual(idx.typed("Nice 👍:skin-tone-5:"), (5, "👍🏾"))
        for text in ("a:b:", "at 10:30:", "std::x:", ":no_such_emoji:"):
            self.assertIsNone(idx.typed(text), text)
        text = "LGTM :+1::skin-tone-2: x:+1: :x: `a[:tada:]`\n```\n:tada:\n```\n:white_check_mark:"
        self.assertEqual(idx.emojize(text), "LGTM 👍🏻 x:+1: :x: `a[:tada:]`\n```\n:tada:\n```\n✅")


class TerminalWidthTest(unittest.TestCase):
    def test_emoji_take_the_cells_tmux_gives_them(self):
        # Measured in tmux 3.8 (variation-selector-always-wide on): after terminal_safe, tmux, ncurses
        # (wcwidth) and lazygerrit count the same cells for each.
        cases = {"❤️": ("❤", 1), "👍🏽": ("👍", 2), "👩‍💻": ("👩💻", 4), "🏳️‍🌈": ("🏳🌈", 3), "1️⃣": ("1⃣", 1), "🇸🇪": ("🇸🇪", 2)}
        for text, (drawn, cells) in cases.items():
            self.assertEqual((terminal_safe(text), text_width(text), text_width(drawn)), (drawn, cells, cells), text)
        self.assertEqual(terminal_safe("क्\u200dष"), "क्\u200dष")  # joiners in scripts are kept
        canvas = VirtualCanvas(12, 1)
        canvas.put(0, 0, "👩‍💻❤️👍🏽|")
        self.assertEqual(canvas.chars[0][7], "|")

    def test_ncurses_gets_emoji_it_counts_like_the_terminal(self):
        drawn = []
        canvas = object.__new__(CursesCanvas)
        canvas.height, canvas.width, canvas.has_colors, canvas.attr_cache = 1, 40, False, {}
        canvas.scr = types.SimpleNamespace(addstr=lambda y, x, text, attr: drawn.append(text))
        canvas.put(0, 0, "ok ❤️ 👍🏽")  # printable, so only terminal_safe() catches it
        canvas.put(0, 10, "👩‍💻")
        self.assertEqual(drawn, ["ok ❤ 👍", "👩💻"])


class SlackWorkspaceTest(unittest.TestCase):
    def serve(self, body):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/api/emoji.list", seen

    def test_fetch_and_cache(self):
        url, seen = self.serve({"ok": True, "emoji": {"party-parrot": "https://x/p.gif", "yes": "alias:+1", "bad": 1}})
        emoji = fetch_workspace("xoxp-1", url=url)
        self.assertEqual((emoji, seen), ({"party-parrot": "https://x/p.gif", "yes": "alias:+1"}, ["Bearer xoxp-1"]))
        tmp = Path(tempfile.mkdtemp(prefix="lazygerrit-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "cache" / "slack-emoji.json"
        write_cache("xoxp-1", emoji, path)
        cached, age = read_cache("xoxp-1", path)
        self.assertEqual(cached, emoji)
        self.assertLess(age, 60)
        self.assertEqual(read_cache("xoxp-2", path)[0], None)  # another token: another workspace
        self.assertEqual(parse_workspace({"thing": "alias:+1"}), {"thing": "alias:+1"})
        with self.assertRaises(ValueError):
            parse_workspace(["not", "emoji"])

    def test_refused(self):
        url, _ = self.serve({"ok": False, "error": "missing_scope"})
        with self.assertRaisesRegex(SlackError, "emoji:read"):
            fetch_workspace("xoxb-1", url=url)


if __name__ == "__main__":
    unittest.main()
