import unittest

from lazygerrit.diff import LineRef, file_as_added, parse_unified
from lazygerrit.models import Account, Comment, Thread
from lazygerrit.ui import render
from lazygerrit.ui.layout import compute_layout
from lazygerrit.ui.text import Style, ansi_to_lines, drop_first_char, plain, sanitize, text_block, text_width, truncate, wrap
from lazygerrit.ui.widgets import ListPanel

DIFF = """diff --git a/q.sql b/q.sql
index 1..2 100644
--- a/q.sql
+++ b/q.sql
@@ -1,4 +1,4 @@
 select 1;
--- old comment
+-- new comment
 select 2;
 select 3;
"""


class DiffTest(unittest.TestCase):
    def test_deleted_line_starting_with_dashes_is_not_a_header(self):
        rows = parse_unified(DIFF)
        kinds = [(r.kind, r.old, r.new) for r in rows if r.kind not in ("meta", "hunk")]
        self.assertEqual(kinds, [("ctx", 1, 1), ("del", 2, None), ("add", None, 2), ("ctx", 3, 3), ("ctx", 4, 4)])
        self.assertEqual(rows[6].text, "-- old comment")

    def test_line_refs_and_inline_threads(self):
        rows = parse_unified(DIFF)
        c = Comment("q.sql", 2, Account("Robin"), "why?", 1, unresolved=True)
        base = Comment("q.sql", 2, Account("Kim"), "old side", 1)
        threads = [Thread("q.sql", 2, 1, [c], True), Thread("q.sql", 2, 1, [base], False, side="PARENT")]
        lines, meta = render.diff_lines("q.sql", rows, threads, 80)
        refs = [(m.path, m.line, m.side) for m in meta if isinstance(m, LineRef)]
        self.assertIn(("q.sql", 2, "PARENT"), refs)
        self.assertIn(("q.sql", 2, None), refs)
        text = ["".join(t for t, _ in ln) for ln in lines]
        del_idx = next(i for i, ln in enumerate(text) if "-- old comment" in ln)
        add_idx = next(i for i, ln in enumerate(text) if "-- new comment" in ln)
        self.assertIn("old side", text[del_idx + 2])
        self.assertIn("why?", text[add_idx + 2])

    def test_intraline_highlights_only_the_changed_words(self):
        rows = parse_unified("@@ -1,1 +1,1 @@\n-    out = crop(path, 0, 100)\n+    out = crop(path, 0, 128)\n")
        lines, _ = render.diff_lines("a.py", rows, [], 80)
        emphasized = [[t for t, st in ln if st.bg != -1] for ln in lines if any(st.bg != -1 for _, st in ln)]
        self.assertEqual(emphasized, [["100"], ["128"]])

    def test_highlighted_lines_must_match_the_diff_line_for_line(self):
        rows = parse_unified(DIFF)
        lines = DIFF.split("\n")
        colored = "\n".join(
            "\x1b[48;5;22m+\x1b[38;5;81m-- NEW comment\x1b[0m" if ln == "+-- new comment" else f"\x1b[38;5;81m{ln}\x1b[0m"
            for ln in lines
        )
        styles = render.highlight_rows(rows, DIFF, colored)
        texts = {rows[i].text: styled for i, styled in styles.items()}
        self.assertNotIn("-- new comment", texts)  # its text was changed, so it is shown as usual
        self.assertEqual(texts["select 1;"], ([("select 1;", Style(fg=81))], None))
        self.assertEqual(texts["-- old comment"][1], None)
        self.assertEqual(render.highlight_rows(rows, DIFF, "\n".join(lines[1:])), {})

    def test_commit_message_as_added_file(self):
        rows = file_as_added("Parent: x\n\nSubject\n")
        self.assertEqual([(r.kind, r.new) for r in rows], [("add", 1), ("add", 2), ("add", 3)])


class TextTest(unittest.TestCase):
    def test_wide_characters(self):
        self.assertEqual(text_width("🟦 ok"), 5)
        self.assertEqual(text_width("✓"), 1)

    def test_truncate_adds_ellipsis(self):
        ln = truncate([("abcdef", None), ("ghij", None)], 6)
        self.assertEqual("".join(t for t, _ in ln), "abcde…")

    def test_wrap_keeps_indent(self):
        self.assertEqual(wrap("  aaa bbb ccc", 9), ["  aaa bbb", "  ccc"])

    def test_multiline_text_never_reaches_the_terminal(self):
        self.assertEqual(sanitize("Error: x\n\n$ gerrit query"), "Error: x  $ gerrit query")
        self.assertEqual([ln[0][0] for ln in text_block("a\n\nb")], ["a", "", "b"])

    def test_ansi_tabs_and_markers(self):
        colored = "\x1b[48;5;22m+\x1b[31m\tif\x1b[0m\tx"
        self.assertEqual(plain(ansi_to_lines(colored)[0]), sanitize("+\tif\tx"))
        self.assertEqual(plain(ansi_to_lines(drop_first_char(colored))[0]), sanitize("\tif\tx"))
        self.assertEqual(ansi_to_lines(colored)[0][0], ("+", Style(bg=22)))

    def test_ansi_colors(self):
        (ln,) = ansi_to_lines("\x1b[32m+added\x1b[0m plain \x1b[38;5;196mred\x1b[0m")
        self.assertEqual([(t, s.fg) for t, s in ln], [("+added", 2), (" plain ", -1), ("red", 196)])


class ListPanelTest(unittest.TestCase):
    def panel(self, items):
        p = ListPanel("t")
        p.key_fn = lambda it: it
        p.set_items(items)
        return p

    def test_range_is_dropped_when_a_reload_reorders_it(self):
        p = self.panel(["A", "B", "C"])
        p.goto(1)
        p.toggle_range()
        p.move(1)
        self.assertEqual(p.selection(), ["B", "C"])
        p.set_items(["C", "A", "B"])
        self.assertEqual(p.selection(), ["C"])

    def test_range_survives_a_reload_that_keeps_its_items(self):
        p = self.panel(["A", "B", "C"])
        p.goto(1)
        p.toggle_range()
        p.move(1)
        p.set_items(["A", "C", "B", "D"])
        self.assertEqual(sorted(p.selection()), ["B", "C"])


class LayoutTest(unittest.TestCase):
    def test_lazygit_proportions(self):
        r = compute_layout(150, 45, focused="changes", expand="changes")
        self.assertEqual(r["status"].h, 3)
        self.assertEqual(r["changes"].w, 49)
        self.assertEqual(r["main"].x, 49)
        self.assertEqual(r["cmdlog"].h, 8)
        self.assertEqual(sum(r[n].h for n in ("status", "changes", "stack", "files", "comments")), 44)
        self.assertGreater(r["changes"].h, r["stack"].h)

    def test_accordion_on_short_terminals(self):
        r = compute_layout(120, 20, focused="files", expand="files")
        self.assertEqual([r[n].h for n in ("changes", "stack", "comments")], [3, 3, 3])
        self.assertEqual(r["files"].h, 19 - 3 - 9)

    def test_fullscreen(self):
        r = compute_layout(120, 40, focused="main", screen_mode=2)
        self.assertEqual(set(r), {"main", "bottom"})


if __name__ == "__main__":
    unittest.main()
