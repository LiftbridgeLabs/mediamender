"""Read the UI as one document.

The stylesheet and behaviour moved out of templates/index.html into
static/app.css and static/app.js. Tests that assert "the UI contains X" mean
the whole UI, not one of its files, so they read it through here.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def template_text() -> str:
    return (REPO / "templates" / "index.html").read_text(encoding="utf-8")


def script_text() -> str:
    return (REPO / "static" / "app.js").read_text(encoding="utf-8")


def style_text() -> str:
    return (REPO / "static" / "app.css").read_text(encoding="utf-8")


def ui_text() -> str:
    """Markup, styles and behaviour concatenated, as the browser sees them."""
    return "\n".join((template_text(), style_text(), script_text()))


def rendered_ui(client) -> str:
    """A rendered response plus the static assets that page pulls in."""
    html = client.get("/").get_data(as_text=True)
    return "\n".join((html, style_text(), script_text()))
