from pathlib import Path
import os
import shutil

import pytest


MODULE_MARKERS = {
    "test_lights.py": ("lights",),
    "test_demo.py": ("demo",),
    "test_registration.py": ("registration",),
    "test_rendezvous.py": ("registration", "pilot"),
    "test_state_updates.py": ("registration", "pilot"),
    "test_website_controls.py": ("registration", "pilot"),
    "test_website.py": ("registration", "people"),
    "test_mail_signing.py": ("registration",),
    "test_passenger.py": ("registration", "people", "pilot"),
    "test_owner_notifications.py": ("registration", "people"),
    "test_people.py": ("people",),
    "test_invitation_pdf.py": ("cards",),
    "test_pilot.py": ("pilot",),
    "test_hosted_tls.py": ("demo", "pilot"),
    "test_fleet.py": ("pilot", "registration"),
    "test_home_runner.py": ("pilot",),
    "test_tooling.py": ("tooling",),
}


@pytest.fixture
def php_runtime():
    selected = os.environ.get('BOOMER_TEST_PHP') or shutil.which('php')
    path = Path(selected) if selected else Path(__file__).resolve().parents[1] / 'data/php-runtime/bin/php.exe'
    if not path.is_file():
        pytest.skip('The optional native PHP runtime is unavailable.')
    return path


def pytest_collection_modifyitems(items):
    for item in items:
        relative = Path(item.path).relative_to(Path(__file__).parent).as_posix()
        for marker in MODULE_MARKERS.get(relative, ()):
            item.add_marker(getattr(pytest.mark, marker))
