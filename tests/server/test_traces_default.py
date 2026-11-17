"""The test suite never writes the real ~/.armature/traces.db, from this process or the ones it starts."""
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

from armature.runtime import engine

PRINT_DEFAULT = "from armature.runtime import engine; print(engine._GLOBAL_TRACES_DB.expanduser())"


def test_the_default_traces_db_is_under_a_temporary_directory_here_and_in_a_child():
    temporary = [Path(d).resolve() for d in (tempfile.gettempdir(), "/tmp")]
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    child = subprocess.run([sys.executable, "-c", PRINT_DEFAULT], capture_output=True, text=True, check=True,
                           timeout=60)
    for found in (engine._GLOBAL_TRACES_DB.expanduser().resolve(), Path(child.stdout.strip()).resolve()):
        assert any(found.is_relative_to(d) for d in temporary), found
        assert not found.is_relative_to(real_home / ".armature"), found
