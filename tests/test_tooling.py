from pathlib import Path

from conftest import MODULE_MARKERS


def test_every_test_module_has_current_domain_markers():
    root = Path(__file__).parent
    discovered = {path.relative_to(root).as_posix() for path in root.rglob("test_*.py")}
    assert discovered == set(MODULE_MARKERS)
    assert all(set(markers) & {"lights", "tooling"} for markers in MODULE_MARKERS.values())
