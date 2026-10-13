"""ccm sharing detection, shell detection, aliases. Uses a temp CCM_HOME only."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccm  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        p = mock.patch.dict(os.environ, {"CCM_HOME": str(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def mkdir(self, rel, content=True):
        p = self.home / rel
        p.mkdir(parents=True, exist_ok=True)
        if content:
            (p / "x.md").write_text("x")
        return p

    def link(self, rel, target):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p, target_is_directory=True)


class DefaultSourceLayout(Base):
    def setUp(self):
        super().setUp()
        for d in ("skills", "agents", "rules"):
            self.mkdir(f".claude/{d}")
        for prof in ("personal", "work", "codex"):
            self.mkdir(f".claude-{prof}", content=False)
            for d in ("skills", "agents", "rules"):
                self.link(f".claude-{prof}/{d}", self.home / ".claude" / d)

    def test_default_is_source_others_linked(self):
        rep = ccm.sharing_report()
        for d in ("skills", "agents", "rules"):
            self.assertEqual(rep[d]["shared_source"], str(self.home / ".claude" / d))
            st = rep[d]["statuses"]
            self.assertEqual(st["default"], "source")
            for n in ("personal", "work", "codex"):
                self.assertEqual(st[n], "linked")
        self.assertIsNone(rep["commands"]["shared_source"])
        self.assertEqual(set(rep["commands"]["statuses"].values()), {"missing"})

    def test_broken_and_separate(self):
        os.unlink(self.home / ".claude-work" / "rules")
        self.link(".claude-work/rules", self.home / "nowhere")
        os.unlink(self.home / ".claude-codex" / "agents")
        self.mkdir(".claude-codex/agents")
        rep = ccm.sharing_report()
        self.assertEqual(rep["rules"]["statuses"]["work"], "broken")
        self.assertEqual(rep["agents"]["statuses"]["codex"], "separate")
        self.assertEqual(rep["agents"]["statuses"]["personal"], "linked")

    def test_link_fixes_missing_to_default(self):
        os.unlink(self.home / ".claude-work" / "skills")
        with redirect_stdout(io.StringIO()):
            rc = ccm.main(["link", "work"])
        self.assertEqual(rc, 0)
        lnk = self.home / ".claude-work" / "skills"
        self.assertTrue(ccm.is_link(lnk))
        self.assertEqual(os.path.realpath(lnk), str(self.home / ".claude" / "skills"))
        self.assertFalse(ccm.is_link(self.home / ".claude" / "skills"))
        # commands has no source: skipped, not created
        self.assertFalse((self.home / ".claude-work" / "commands").exists())

    def test_link_never_replaces_real_dir_or_source(self):
        os.unlink(self.home / ".claude-work" / "agents")
        mine = self.mkdir(".claude-work/agents")
        with redirect_stdout(io.StringIO()):
            rc = ccm.main(["link", "--all"])
        self.assertEqual(rc, 1)
        self.assertFalse(ccm.is_link(mine))
        self.assertTrue((mine / "x.md").exists())
        self.assertFalse(ccm.is_link(self.home / ".claude" / "skills"))

    def test_report_shapes(self):
        lr = ccm.layout_report()
        self.assertEqual(set(lr), {"profiles", "shared_sources", "shared_root", "shared_exists",
                                   "shared_dirs", "platform", "shell", "rc_file"})
        self.assertFalse(lr["shared_exists"])
        prof = {p["name"]: p for p in lr["profiles"]}
        self.assertEqual(set(prof["work"]), {"name", "path", "credentials", "sessions", "shared",
                                             "shared_source", "linked", "alias"})
        self.assertEqual(prof["work"]["linked"], 3)
        self.assertEqual(prof["default"]["linked"], 3)
        self.assertEqual(set(prof["work"]["alias"]), {"primary", "other", "installed", "installed_name"})


class SharedHomeLayout(Base):
    def test_shared_layout(self):
        for d in ccm.SHARED_DIRS:
            self.mkdir(f".claude-shared/{d}")
        self.mkdir(".claude", content=False)
        self.mkdir(".claude-work", content=False)
        for prof in (".claude", ".claude-work"):
            for d in ccm.SHARED_DIRS:
                self.link(f"{prof}/{d}", self.home / ".claude-shared" / d)
        rep = ccm.sharing_report()
        for d in ccm.SHARED_DIRS:
            self.assertEqual(rep[d]["shared_source"], str(self.home / ".claude-shared" / d))
            self.assertEqual(set(rep[d]["statuses"].values()), {"linked"})

    def test_shared_exists_nothing_linked(self):
        self.mkdir(".claude-shared/skills")
        self.mkdir(".claude-work", content=False)
        rep = ccm.sharing_report()
        self.assertEqual(rep["skills"]["shared_source"], str(self.home / ".claude-shared" / "skills"))
        self.assertEqual(rep["skills"]["statuses"]["work"], "missing")

    def test_link_prefers_shared_home(self):
        self.mkdir(".claude/skills")
        self.mkdir(".claude-shared/skills", content=False)
        self.mkdir(".claude-work", content=False)
        with redirect_stdout(io.StringIO()):
            ccm.main(["link", "work"])
        self.assertEqual(os.path.realpath(self.home / ".claude-work" / "skills"),
                         str(self.home / ".claude-shared" / "skills"))

    def test_no_links_no_shared(self):
        self.mkdir(".claude/skills")
        self.mkdir(".claude-work/skills")
        rep = ccm.sharing_report()
        self.assertIsNone(rep["skills"]["shared_source"])
        self.assertEqual(set(rep["skills"]["statuses"].values()), {"separate"})


class ShellTests(Base):
    def test_detect(self):
        d = ccm.detect_shell({"SHELL": "/bin/zsh"}, "posix", "darwin")
        self.assertEqual((d["platform"], d["shell"]), ("darwin", "zsh"))
        self.assertEqual(d["rc_file"], str(self.home / ".zshrc"))
        d = ccm.detect_shell({"SHELL": "/bin/zsh", "ZDOTDIR": str(self.home / "zd")}, "posix", "linux")
        self.assertEqual(d["rc_file"], str(self.home / "zd" / ".zshrc"))
        d = ccm.detect_shell({"SHELL": "/usr/bin/bash"}, "posix", "linux")
        self.assertEqual((d["shell"], d["rc_file"]), ("bash", str(self.home / ".bashrc")))
        (self.home / ".bash_profile").write_text("")
        d = ccm.detect_shell({"SHELL": "/bin/bash"}, "posix", "darwin")
        self.assertEqual(d["rc_file"], str(self.home / ".bash_profile"))
        d = ccm.detect_shell({"SHELL": "/bin/bash"}, "posix", "linux")
        self.assertEqual(d["rc_file"], str(self.home / ".bashrc"))
        d = ccm.detect_shell({"SHELL": "/opt/homebrew/bin/fish"}, "posix", "darwin")
        self.assertEqual((d["shell"], d["rc_file"]),
                         ("fish", str(self.home / ".config" / "fish" / "config.fish")))
        d = ccm.detect_shell({}, "nt", "win32")
        self.assertEqual((d["platform"], d["shell"]), ("windows", "powershell"))
        self.assertTrue(d["rc_file"].endswith("Microsoft.PowerShell_profile.ps1"))
        d = ccm.detect_shell({"PROFILE": "C:/p/profile.ps1"}, "nt", "win32")
        self.assertIn("profile.ps1", d["rc_file"])

    def test_alias_lines(self):
        self.assertEqual(ccm.alias_lines("work", "zsh"),
                         "alias claude-work='CLAUDE_CONFIG_DIR=\"$HOME/.claude-work\" claude'")
        self.assertEqual(ccm.alias_lines("work", "bash"), ccm.alias_lines("work", "zsh"))
        self.assertEqual(ccm.alias_lines("work", "fish"),
                         "alias claude-work 'CLAUDE_CONFIG_DIR=\"$HOME/.claude-work\" claude'")
        self.assertEqual(ccm.alias_lines("default", "fish"),
                         "alias claude-default 'CLAUDE_CONFIG_DIR=\"$HOME/.claude\" claude'")
        self.assertEqual(ccm.alias_lines("work", "powershell"),
                         "function claude-work { $env:CLAUDE_CONFIG_DIR=\"$HOME\\.claude-work\"; claude @args }")

    def test_alias_installed(self):
        rc = self.home / "rc"
        self.assertFalse(ccm.alias_installed("work", str(rc)))
        rc.write_text("alias claude-work='x'\nalias claude-work2='y'\nfunction claude-fn { }\n")
        self.assertTrue(ccm.alias_installed("work", str(rc)))
        self.assertTrue(ccm.alias_installed("fn", str(rc)))
        self.assertFalse(ccm.alias_installed("personal", str(rc)))
        rc.write_text("alias claude-work2='y'\n# alias claude-work\n")
        self.assertFalse(ccm.alias_installed("work", str(rc)))
        rc.write_text("alias claude-work 'z'\n")
        self.assertTrue(ccm.alias_installed("work", str(rc)))

    def test_alias_found_by_config_dir(self):
        # the user's real layout: the codex profile is launched as `claudex`
        rc = self.home / "rc"
        rc.write_text("alias claude='X=1 claude'\n"
                      "alias claudex='CLAUDE_CONFIG_DIR=$HOME/.claude-codex claude'\n"
                      "alias cw='CLAUDE_CONFIG_DIR=$HOME/.claude-codex2 claude'\n")
        self.assertEqual(ccm.installed_alias("codex", str(rc)), "claudex")
        self.assertIsNone(ccm.installed_alias("codex2x", str(rc)))
        self.assertEqual(ccm.installed_alias("codex2", str(rc)), "cw")
        self.assertIsNone(ccm.installed_alias("work", str(rc)))
        self.assertEqual(ccm.installed_alias("default", str(rc)), "claude")  # plain `claude` is ~/.claude

    def test_alias_block(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            b = ccm.alias_block("work")
        self.assertEqual(b["primary"]["shell"], "zsh")
        self.assertEqual(set(b["other"]), {"bash", "fish", "powershell"})
        self.assertFalse(b["installed"])
        b = ccm.alias_block("work", "fish")
        self.assertEqual(b["primary"]["shell"], "fish")
        self.assertEqual(set(b["other"]), {"zsh", "bash", "powershell"})


if __name__ == "__main__":
    unittest.main()
