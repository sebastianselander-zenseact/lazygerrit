import tempfile
import textwrap
import unittest
from pathlib import Path

from lazygerrit.srcrepo import (
    discover_run_selected_jobs,
    load_hashtags,
    prio_high_hashtags,
    run_selected_comment,
)

YAML = textwrap.dedent(
    """\
    - project:
        check:
          jobs:
            - src@not_selectable
        run-selected:
          jobs:
            - src@ada_rss_feed:
                branches: ^(master$|rel_AD_ADAS_)
            - src@second_job
        gate:
          jobs:
            - src@gate_only
    """
)


class SrcRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "src_config/premerge/dos65").mkdir(parents=True)
        (self.repo / "src_config/premerge/setup").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovers_only_run_selected_jobs(self):
        (self.repo / "src_config/premerge/a.yaml").write_text(YAML)
        (self.repo / "src_config/premerge/dos65/b.yaml").write_text(
            "- project:\n    run-selected:\n      jobs:\n        - src@dos65_job:\n"
        )
        (self.repo / "src_config/premerge/setup/c.yaml").write_text(
            "- project:\n    run-selected:\n      jobs:\n        - src@ignored\n"
        )
        self.assertEqual(discover_run_selected_jobs(self.repo), ["ada_rss_feed", "dos65_job", "second_job"])

    def test_run_selected_comment_matches_rng(self):
        self.assertEqual(run_selected_comment("src", ["a", "src@b"]), "run-selected:\nsrc@a\nsrc@b")

    def test_hashtags_read_from_repo(self):
        helpers = self.repo / "support/ci_helpers"
        helpers.mkdir(parents=True)
        (helpers / "gerrit_hashtags.py").write_text('PATCH_PRIORITY_HIGH_HASHTAG = "prio_high_v2"\n')
        self.assertEqual(load_hashtags(self.repo)["PATCH_PRIORITY_HIGH_HASHTAG"], "prio_high_v2")
        self.assertEqual(prio_high_hashtags(self.repo), ["ci_prio_check", "ci_prio_flush_gate", "prio_high_v2"])
        self.assertEqual(prio_high_hashtags(None), ["ci_prio_check", "ci_prio_flush_gate", "prio_high"])


if __name__ == "__main__":
    unittest.main()
