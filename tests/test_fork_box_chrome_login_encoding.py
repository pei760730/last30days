"""Keep the box-chrome SKILL reader independent of the host text encoding."""

from pathlib import Path

import pytest

import test_box_chrome_login as login_contract


def test_skill_reader_preserves_utf8_under_cp950_default(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    expected = "UTF-8 skill: \u96ea\u2603\U0001f680\n"
    skill.write_bytes(expected.encode("utf-8"))
    # This fixture must expose the Windows decoder failure, on every host.
    with pytest.raises(UnicodeDecodeError):
        skill.read_text(encoding="cp950")

    read_text = Path.read_text

    def cp950_read_text(path, encoding=None, errors=None, **kwargs):
        # Change only the fixture's implicit encoding; keep real file decoding.
        if path == skill and encoding is None:
            encoding = "cp950"
        return read_text(path, encoding=encoding, errors=errors, **kwargs)

    monkeypatch.setattr(login_contract, "SKILL", skill)
    monkeypatch.setattr(Path, "read_text", cp950_read_text)
    assert login_contract._skill() == expected
