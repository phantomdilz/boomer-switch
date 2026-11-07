import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import MODULE_MARKERS


def test_every_test_module_has_current_domain_markers():
    root = Path(__file__).parent
    discovered = {path.relative_to(root).as_posix() for path in root.rglob("test_*.py")}
    assert discovered == set(MODULE_MARKERS)
    assert all(set(markers) & {"lights", "demo", "registration", "people", "cards", "tooling"} for markers in MODULE_MARKERS.values())


@pytest.mark.parametrize("options", [
    ("--basetemp", "{target}"),
    ("--basetemp={target}",),
    ("-o", "cache_dir={target}"),
    ("-ocache_dir={target}",),
    ("--override-ini=cache_dir={target}",),
    ("--override-ini", "addopts=--basetemp={target}"),
    ("--", "--basetemp={target}"),
])
def test_runner_rejects_path_overrides_without_deleting_sentinel(tmp_path, options):
    target = tmp_path / "keep"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("SYNTHETIC_KEEP", encoding="utf-8")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "run_tests.py"), "tooling", "--workers", "2",
         *(option.format(target=target) for option in options)],
        cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "The runner owns" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "SYNTHETIC_KEEP"
    assert list(target.iterdir()) == [sentinel]


def test_inherited_pytest_options_cannot_replace_owned_paths(tmp_path):
    target = tmp_path / "keep"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("SYNTHETIC_KEEP", encoding="utf-8")
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTEST_ADDOPTS"] = f'--basetemp="{target}" -o cache_dir="{target}"'
    result = subprocess.run(
        [sys.executable, str(root / "run_tests.py"), "tooling", "--workers", "2",
         "tests/test_tooling.py::test_every_test_module_has_current_domain_markers", "-q"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "SYNTHETIC_KEEP"
    assert list(target.iterdir()) == [sentinel]
