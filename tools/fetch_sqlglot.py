"""Download sqlglot at the pinned commit from its own repository into /src/sqlglot."""

import io
import os
import sys
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dest = sys.argv[1] if len(sys.argv) > 1 else "/src"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz").read()
tarfile.open(fileobj=io.BytesIO(data)).extractall(dest, filter="data")
os.rename(os.path.join(dest, f"sqlglot-{SHA}"), os.path.join(dest, "sqlglot"))
