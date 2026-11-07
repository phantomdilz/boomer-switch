from pathlib import Path

import pytest


MODULE_MARKERS = {
    "test_lights.py": ("lights",),
    "test_tooling.py": ("tooling",),
}


def pytest_collection_modifyitems(items):
    for item in items:
        relative = Path(item.path).relative_to(Path(__file__).parent).as_posix()
        for marker in MODULE_MARKERS.get(relative, ()):
            item.add_marker(getattr(pytest.mark, marker))
