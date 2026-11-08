from pathlib import Path
import shutil
import subprocess

import pytest


def test_mobile_controls_behavior_in_javascript():
    node = shutil.which('node')
    if node is None:
        pytest.skip('The optional JavaScript runtime is unavailable.')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root/'tests/website_controls_cases.cjs'), str(root/'boomer_switch/web/website.js')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
