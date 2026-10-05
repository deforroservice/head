"""Inline web/engine.js into web/sandbox.html -> web/dist/deforro-dds-sandbox.html.

The result is a single self-contained page (published as the sandbox artifact; it can
also be opened straight from disk).
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent


def build() -> Path:
    page = (HERE / "sandbox.html").read_text(encoding="utf-8")
    engine = (HERE / "engine.js").read_text(encoding="utf-8")
    if "</script" in engine.lower():
        raise SystemExit("engine.js must not contain a closing script tag")
    out = HERE / "dist" / "deforro-dds-sandbox.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page.replace("/*ENGINE*/", engine, 1), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(build())
