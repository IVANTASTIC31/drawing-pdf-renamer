from pathlib import Path

import pytest

from drawing_renamer.naming import build_filename, sanitize_component, validate_destination


def test_slash_is_replaced_with_hyphen() -> None:
    assert build_filename("B.0153.002", "大封头", "CP41.101/2") == "B.0153.002_大封头_CP41.101-2.pdf"


def test_all_windows_illegal_characters_are_safe() -> None:
    assert sanitize_component(' A\\B:C*D?E"F<G>H|I/J ') == "A-B-C-D-E-F-G-H-I-J"


def test_empty_component_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_filename("B.001", "", "A10")


def test_existing_destination_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "old.pdf"
    source.write_bytes(b"source")
    (tmp_path / "new.pdf").write_bytes(b"destination")
    assert "已存在" in (validate_destination(source, "new.pdf") or "")
