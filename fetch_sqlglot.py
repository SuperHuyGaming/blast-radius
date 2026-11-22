"""Fetch one sqlglot commit, depth 1, into the current directory and check it out.

Runs at image build, where the base image has no git: dulwich speaks the git
protocol to sqlglot's own repository.
"""

import sys

from dulwich import porcelain
from dulwich.client import get_transport_and_path
from dulwich.repo import Repo

URL = "https://github.com/tobymao/sqlglot.git"

sha = sys.argv[1].encode()
repo = Repo.init(".")
client, path = get_transport_and_path(URL)
client.fetch(path, repo, determine_wants=lambda refs, depth=None: [sha], depth=1)
porcelain.reset(repo, "hard", sha)
if repo.head() != sha:
    sys.exit(f"checked out {repo.head()!r}, wanted {sha!r}")
repo.close()
