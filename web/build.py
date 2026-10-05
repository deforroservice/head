"""Build the sandbox page from web/sandbox.html + web/engine.js.

Writes two self-contained pages:
  web/dist/deforro-dds-sandbox.html  page body for embedded previews (the host adds the HTML skeleton)
  docs/index.html                    complete document for GitHub Pages / any static host
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%230c3a2b'/%3E"
    "%3Cpath d='M9 22c0-8 6-13 14-13-1 8-6 13-14 13z' fill='%238de2bd'/%3E%3C/svg%3E"
)

SITE_HEAD = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="description" content="DEFORRO sandbox: check a shipment export against EUDR rules and file it to a simulated TRACES.">
<link rel="icon" href="{FAVICON}">
<style>body{{margin:0}}[hidden]{{display:none!important}}</style>
"""


def render(site: bool) -> str:
    page = (HERE / "sandbox.html").read_text(encoding="utf-8")
    engine = (HERE / "engine.js").read_text(encoding="utf-8")
    if "</script" in engine.lower():
        raise SystemExit("engine.js must not contain a closing script tag")
    page = page.replace("/*ENGINE*/", engine, 1)
    page = page.replace("/*SITE*/", "window.DEFORRO_SITE = true;" if site else "", 1)
    if not site:
        return page
    # Everything up to </style> is head material; the rest is the body.
    head, body = page.split("</style>", 1)
    return f"{SITE_HEAD}{head}</style>\n</head>\n<body>{body}</body>\n</html>\n"


def build() -> list[Path]:
    outputs = {
        HERE / "dist" / "deforro-dds-sandbox.html": render(site=False),
        ROOT / "docs" / "index.html": render(site=True),
    }
    for path, text in outputs.items():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(outputs)


if __name__ == "__main__":
    for p in build():
        print(p)
