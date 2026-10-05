"""docs/index.html is what GitHub Pages serves: keep it in sync with the sources."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build_module():
    spec = importlib.util.spec_from_file_location("web_build", ROOT / "web" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_page_is_up_to_date():
    expected = _build_module().render(site=True)
    actual = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert actual == expected, "docs/index.html is stale: run python web/build.py"


def test_published_page_is_unbranded_and_not_indexed():
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8").lower()
    assert "claude" not in page and "anthropic" not in page
    assert '<meta name="robots" content="noindex, nofollow">' in page
    assert "window.deforro_site = true" in page
