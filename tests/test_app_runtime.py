from pathlib import Path

from drawing_renamer import app


def test_frozen_runtime_uses_bundled_paddle_models(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cache = tmp_path / "paddlex_cache"
    cache.mkdir()
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", "C:/wrong-cache")

    app.configure_runtime_environment()

    assert app.os.environ["PADDLE_PDX_CACHE_HOME"] == str(cache)


def test_source_runtime_logo_is_available() -> None:
    icon_path = app.application_icon_path()

    assert icon_path.name == "app-logo.png"
    assert icon_path.is_file()
