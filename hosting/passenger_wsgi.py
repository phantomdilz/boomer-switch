"""Copy this startup file into the private Passenger application directory."""

from pathlib import Path

from boomer_switch.website_passenger import load_application


application = load_application(Path(__file__).resolve().parent / "config.local.passenger.json")
