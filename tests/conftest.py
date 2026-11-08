from pathlib import Path

import pytest


MODULE_MARKERS = {
    "test_lights.py": ("lights",),
    "test_demo.py": ("demo",),
    "test_registration.py": ("registration",),
    "test_rendezvous.py": ("registration", "pilot"),
    "test_website.py": ("registration", "people"),
    "test_passenger.py": ("registration", "people", "pilot"),
    "test_people.py": ("people",),
    "test_invitation_pdf.py": ("cards",),
    "test_pilot.py": ("pilot",),
    "test_guest_proxy.py": ("pilot",),
    "test_hosted_tls.py": ("demo", "pilot"),
    "test_home_runner.py": ("pilot",),
    "test_tooling.py": ("tooling",),
}


def pytest_collection_modifyitems(items):
    for item in items:
        relative = Path(item.path).relative_to(Path(__file__).parent).as_posix()
        for marker in MODULE_MARKERS.get(relative, ()):
            item.add_marker(getattr(pytest.mark, marker))
