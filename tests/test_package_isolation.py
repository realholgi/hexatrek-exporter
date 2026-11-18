"""The exporter installs and imports without the independent Komoot package."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_every_runtime_module_imports_from_an_isolated_package_copy(tmp_path):
    package = tmp_path / "hexatrek_exporter"
    shutil.copytree(ROOT / "src" / "hexatrek_exporter", package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    script = """
import importlib
import importlib.abc
import pkgutil
from pathlib import Path
import sys

blocked = ("mcp", "kompy", "gpxpy", "openrouteservice", "komoot_mcp")
class ExporterOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Forbidden package dependency: " + fullname)
sys.meta_path.insert(0, ExporterOnly())
sys.path.insert(0, sys.argv[1])
import hexatrek_exporter
assert Path(hexatrek_exporter.__file__).resolve().parent == Path(sys.argv[1]) / "hexatrek_exporter"
for module in pkgutil.walk_packages(hexatrek_exporter.__path__, "hexatrek_exporter."):
    importlib.import_module(module.name)
assert not any(name == prefix or name.startswith(prefix + ".")
               for name in sys.modules for prefix in blocked)
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
