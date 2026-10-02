"""Project identity: one root per working folder, never the home directory."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir

data_dir = isolate()
home = temp_dir("vo-home-")
(home / ".claude").mkdir()              # Claude Code's own config dir
os.environ["HOME"] = str(home)

from voiceover import projects
from voiceover.settings import detect_project_root

git_project = home / "work" / "git-project"
(git_project / ".git").mkdir(parents=True)
(git_project / "src" / "deep").mkdir(parents=True)
claude_project = home / "work" / "claude-project"
(claude_project / ".claude").mkdir(parents=True)
bare = home / "work" / "bare"
bare.mkdir(parents=True)

# --- the home directory's .claude is not a project marker -----------------
check("a git project under home resolves to itself, not to home",
      detect_project_root(str(git_project)) == git_project,
      detect_project_root(str(git_project)))
check("a subdirectory resolves to its project",
      detect_project_root(str(git_project / "src" / "deep")) == git_project,
      detect_project_root(str(git_project / "src" / "deep")))
check("a project with its own .claude still resolves by it",
      detect_project_root(str(claude_project)) == claude_project,
      detect_project_root(str(claude_project)))
check("a folder with no marker resolves to itself, not to home",
      detect_project_root(str(bare)) == bare, detect_project_root(str(bare)))

# --- project_dir: one per root, stable, labelled ---------------------------
a = projects.project_dir(str(git_project))
check("the project dir lives under the data dir",
      a.parent == data_dir / "projects", a)
check("a subdirectory shares its project's dir",
      projects.project_dir(str(git_project / "src")) == a)
check("two projects get two dirs",
      projects.project_dir(str(claude_project)) != a)
key = projects.project_key(str(git_project))
check("the key is 12 hex characters",
      len(key) == 12 and all(c in "0123456789abcdef" for c in key), key)
check("the key is the dir's name", a.name == key, (a.name, key))
check("root.txt names the resolved project root",
      (a / "root.txt").read_text().strip() == str(git_project.resolve()),
      (a / "root.txt").read_text())

# --- session file names ------------------------------------------------------
check("a session id keeps its safe characters",
      projects.session_slug("e0e70ee4-5f95_X") == "e0e70ee4-5f95_X")
check("anything else becomes an underscore",
      projects.session_slug("../a b") == "___a_b", projects.session_slug("../a b"))
check("no session has no slug",
      projects.session_slug(None) is None and projects.session_slug("") is None)

report()
