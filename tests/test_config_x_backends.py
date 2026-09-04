"""CONFIGURATION.md must not advertise X backends the engine does not have (#942)."""

from pathlib import Path

from lib import env

REPO = Path(__file__).resolve().parents[1]


def _x_twitter_row() -> str:
    for line in (REPO / "CONFIGURATION.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("| X / Twitter |"):
            return line
    raise AssertionError("CONFIGURATION.md is missing the X / Twitter table row")


def test_configuration_x_row_omits_scrapecreators():
    row = _x_twitter_row()
    assert "SCRAPECREATORS_API_KEY" not in row
    assert "ScrapeCreators" not in row


def test_engine_has_no_scrapecreators_x_backend():
    assert "scrapecreators" not in env.X_BACKEND_KNOWN
    assert env._x_backend_available("scrapecreators", {"SCRAPECREATORS_API_KEY": "k"}, False) is False
