import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lazygerrit.demo import cleanup_demo, create_demo
from lazygerrit.diff import parse_unified
from lazygerrit.gerrit import Runner
from lazygerrit.renderers import (
    DEFAULT_HIGHLIGHTER,
    DiffRenderer,
    RendererError,
    detect_color_scheme,
    expand_command,
    parse_renderers,
    render,
    run_highlighter,
    syntax_command,
)
from lazygerrit.ui.render import highlight_rows
from lazygerrit.ui.text import strip_ansi

VALUES = {"width": 120, "colorScheme": "dark", "columnWidth": 54, "diffContext": 3}


def py(code: str) -> str:
    return shlex.join([sys.executable, "-c", code])


class TemplateTest(unittest.TestCase):
    def test_variables_with_and_without_dot(self):
        self.assertEqual(expand_command("delta --width={{width}} --{{.colorScheme}}", VALUES), "delta --width=120 --dark")
        self.assertEqual(expand_command("ydiff -w {{ columnWidth }}", VALUES), "ydiff -w 54")

    def test_conditions_from_the_lazygit_docs(self):
        cmd = "delta --paging=never {{if gt .width 160}}--side-by-side{{end}}"
        self.assertEqual(expand_command(cmd, dict(VALUES, width=200)), "delta --paging=never --side-by-side")
        self.assertEqual(expand_command(cmd, VALUES), "delta --paging=never ")
        theme = 'delta --syntax-theme={{if eq .colorScheme "light"}}Github{{else}}Dracula{{end}}'
        self.assertEqual(expand_command(theme, VALUES), "delta --syntax-theme=Dracula")
        self.assertEqual(expand_command(theme, dict(VALUES, colorScheme="light")), "delta --syntax-theme=Github")
        nested = '{{if and (gt .width 100) (ne .colorScheme "light")}}wide-dark{{else if gt .width 100}}wide{{else}}narrow{{end}}'
        self.assertEqual(expand_command(nested, VALUES), "wide-dark")
        self.assertEqual(expand_command(nested, dict(VALUES, colorScheme="light")), "wide")
        self.assertEqual(expand_command(nested, dict(VALUES, width=80)), "narrow")

    def test_mistakes_are_explained(self):
        for bad, why in [
            ("delta {{widht}}", "unknown template variable"),
            ("delta {{if gt .width 1}}x", "without {{end}}"),
            ("delta {{if gt .colorScheme 1}}x{{end}}", "cannot compare"),
            ("delta {{end}}", "without {{if}}"),
        ]:
            with self.assertRaises(RendererError) as e:
                expand_command(bad, VALUES)
            self.assertIn(why, str(e.exception))


class ConfigTest(unittest.TestCase):
    def test_lazygit_style_entries(self):
        renderers, problems = parse_renderers(
            [
                {"command": "delta --{{colorScheme}} --paging=never"},
                {"command": "ydiff -p cat", "colorArg": "never"},
                {"type": "extDiff", "command": "difft --color=always"},
                {"type": "rawGit", "args": ["--color-words"], "name": "color-words"},
                {"type": "builtin"},
                "diff-so-fancy",
            ]
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(r.type, r.label) for r in renderers],
            [
                ("stdinFilter", "delta"),
                ("stdinFilter", "ydiff"),
                ("extDiff", "difft"),
                ("rawGit", "color-words"),
                ("builtin", "review view"),
                ("stdinFilter", "diff-so-fancy"),
            ],
        )
        self.assertEqual(renderers[1].color_arg, "never")

    def test_problems(self):
        renderers, problems = parse_renderers(
            [{"type": "pager", "command": "x"}, {"name": "empty"}, {"command": "delta", "colour": 1}]
        )
        self.assertEqual([r.label for r in renderers], ["delta"])
        self.assertEqual(len(problems), 3)
        self.assertIn("unknown type 'pager'", problems[0])
        self.assertIn("needs a command", problems[1])
        self.assertIn("unknown setting colour", problems[2])

    def test_syntax_highlight_setting(self):
        with mock.patch("shutil.which", return_value="/usr/bin/delta"):
            self.assertEqual(syntax_command("auto"), DEFAULT_HIGHLIGHTER)
            self.assertEqual(syntax_command(True), DEFAULT_HIGHLIGHTER)
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(syntax_command("auto"))
        self.assertIsNone(syntax_command("off"))
        self.assertIsNone(syntax_command(False))
        self.assertEqual(syntax_command("diff-highlight"), "diff-highlight")

    def test_color_scheme(self):
        self.assertEqual(detect_color_scheme("auto", {"COLORFGBG": "0;15"}), "light")
        self.assertEqual(detect_color_scheme("auto", {"COLORFGBG": "15;default;0"}), "dark")
        self.assertEqual(detect_color_scheme("auto", {}), "dark")
        self.assertEqual(detect_color_scheme("light", {"COLORFGBG": "15;0"}), "light")


class TtyRunnerTest(unittest.TestCase):
    def test_output_goes_to_a_terminal_of_the_given_size(self):
        code = "import os, sys\ndata = sys.stdin.read()\nprint(os.isatty(1), os.get_terminal_size(1).columns, data.upper())"
        res = Runner().run_tty([sys.executable, "-c", code], size=(77, 20), input=b"hello")
        self.assertEqual((res.returncode, res.stdout), (0, "True 77 HELLO\n"))

    def test_terminal_queries_are_answered(self):
        code = "import os, sys\nsys.stdout.write('\\x1b]11;?\\x07'); sys.stdout.flush()\nprint(repr(os.read(1, 64)))"
        light = Runner().run_tty([sys.executable, "-c", code], size=(80, 20), dark=False, timeout=10)
        self.assertIn(r"\x1b]11;rgb:ffff/ffff/ffff\x07", light.stdout)

    def test_failures(self):
        self.assertEqual(Runner().run_tty(["sh", "-c", "sleep 5"], size=(80, 20), timeout=0.5).returncode, 124)
        missing = Runner().run_tty(["no-such-diff-tool-xyz"], size=(80, 20))
        self.assertEqual(missing.returncode, 127)


class RenderTest(unittest.TestCase):
    """Renderers on the demo repository: change 100101 ps1 changes crop.py."""

    @classmethod
    def setUpClass(cls):
        cls.be, cls.cfg, cls.server, cls.tmp = create_demo(latency=0)
        cls.git = cls.be.git
        cls.rev = cls.git.git("rev-parse", "refs/demo/c1ps1").strip()
        cls.scripts = Path(tempfile.mkdtemp(prefix="lazygerrit-test-"))
        ext = cls.scripts / "ext.sh"
        ext.write_text('#!/bin/sh\necho "EXT $1 $(basename "$5")"\ncat "$5"\n')
        ext.chmod(0o755)
        cls.ext = str(ext)

    @classmethod
    def tearDownClass(cls):
        cleanup_demo(cls.tmp)
        cleanup_demo(cls.scripts)

    def run_renderer(self, r: DiffRenderer, **kw):
        opts = dict(base=f"{self.rev}^", rev=self.rev, paths=["stringent/rapid/crop.py"])
        opts.update(kw)
        return render(self.git, r, width=77, height=20, context=3, color_scheme="dark", **opts)

    def test_stdin_filter_gets_the_diff_and_a_terminal(self):
        echo = py("import os, sys\nprint('cols', os.get_terminal_size(1).columns)\nsys.stdout.write(sys.stdin.read())")
        status, out = self.run_renderer(DiffRenderer(command=echo))
        self.assertEqual(status, 0)
        self.assertIn("cols 77", out)
        self.assertIn("\x1b[", out)
        self.assertIn('+    LOG.info("Cropping %s", path)', strip_ansi(out))
        status, out = self.run_renderer(DiffRenderer(command=echo, color_arg="never"))
        self.assertNotIn("\x1b[", out)

    def test_external_diff_and_raw_git(self):
        status, out = self.run_renderer(DiffRenderer(type="extDiff", command=self.ext))
        self.assertEqual(status, 0)
        self.assertIn("EXT stringent/rapid/crop.py", out)
        self.assertIn("        data = fh.read()", out)
        status, out = self.run_renderer(DiffRenderer(type="rawGit", args=["--color-words"]))
        self.assertEqual(status, 0)
        self.assertIn('LOG.info("Cropping %s", path)', strip_ansi(out))
        self.assertIn("data = open(path).read()fh.read()", strip_ansi(out))

    def test_texts_such_as_the_commit_message(self):
        texts = ("COMMIT_MSG", "Subject\n\nBody\n", "Subject v2\n\nBody\n")
        status, out = self.run_renderer(DiffRenderer(command="cat", color_arg="never"), texts=texts)
        self.assertEqual(status, 0)
        self.assertIn("diff --git a/COMMIT_MSG b/COMMIT_MSG", out)
        self.assertIn("-Subject\n+Subject v2\n", out)
        status, out = self.run_renderer(DiffRenderer(type="extDiff", command=self.ext), texts=texts)
        self.assertEqual(status, 0)
        self.assertIn("EXT a/COMMIT_MSG COMMIT_MSG\nSubject v2", out)

    def test_failing_renderers_report_their_error(self):
        status, out = self.run_renderer(DiffRenderer(command="no-such-diff-tool-xyz --paging=never"))
        self.assertEqual(status, 127)
        self.assertIn("not found", out)
        status, out = self.run_renderer(DiffRenderer(type="extDiff", command="no-such-diff-tool-xyz"))
        self.assertNotEqual(status, 0)
        self.assertIn("no-such-diff-tool-xyz", out)


class MergeCheckTest(unittest.TestCase):
    """The demo's master on the server has a commit that conflicts with Kim's change 100200."""

    def test_conflicts_are_found_without_writing_to_the_repository(self):
        be, cfg, server, tmp = create_demo(latency=0)
        try:
            git = be.git
            tip = git.remote_tip("master")
            self.assertEqual(tip, server.branch_tip)
            kim, mine = (git.git("rev-parse", f"refs/demo/{n}").strip() for n in ("kim", "c3"))
            self.assertEqual(git.present_commits([kim, mine, "0" * 40]), {kim, mine})
            objects = Path(cfg.repo, ".git", "objects")
            before = sorted(p for p in objects.rglob("*") if p.is_file())
            self.assertEqual(git.merge_conflicts(tip, kim), ["tests/test_spax.py"])
            self.assertEqual(git.merge_conflicts(tip, mine), [])
            self.assertEqual(sorted(p for p in objects.rglob("*") if p.is_file()), before)
        finally:
            be.close()
            cleanup_demo(tmp)


@unittest.skipUnless(shutil.which("delta"), "delta is not installed")
class DeltaHighlightTest(unittest.TestCase):
    def test_delta_colors_every_code_line_of_a_real_diff(self):
        be, cfg, server, tmp = create_demo(latency=0)
        try:
            rev = be.git.git("rev-parse", "refs/demo/c1ps1").strip()
            diff = be.git.diff(f"{rev}^", rev, ["stringent/rapid/crop.py"], 3)
            rows = parse_unified(diff)
            status, colored = run_highlighter(be.git, syntax_command("auto"), diff, color_scheme="dark")
            self.assertEqual(status, 0)
            styles = highlight_rows(rows, diff, colored)
            code = [i for i, r in enumerate(rows) if r.kind in ("add", "del", "ctx")]
            self.assertEqual(sorted(styles), code)
            colors = {s.fg for i in code for _, s in styles[i][0]}
            self.assertGreater(len(colors), 2)  # keywords, strings, names, … in different colors
            added = next(i for i in code if rows[i].kind == "add")
            self.assertIsNotNone(styles[added][1])  # added lines get a background
        finally:
            cleanup_demo(tmp)


if __name__ == "__main__":
    unittest.main()
