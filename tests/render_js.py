"""Write the UI's JavaScript to a file so node can parse and exercise it.

Behaviour lives in static/app.js, but the server-rendered values it reads
arrive through the BOOT blob in templates/index.html. This reassembles the two
into the single script the browser effectively runs.
"""
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def bundle() -> str:
    html = app.app.test_client().get("/").get_data(as_text=True)
    match = re.search(
        r'<script id="mediamender-boot" type="application/json">\s*(.*?)\s*</script>',
        html, re.S,
    )
    if not match:
        raise AssertionError("The page no longer renders a BOOT blob")
    boot = json.loads(match.group(1))
    # The init block, which registers the hashchange listener, is skipped until
    # a config file exists. The harness needs it to run.
    boot["configMissing"] = False
    script = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    # PRODUCT_NAME is declared by the template's inline script, not app.js,
    # so the bundle has to carry it for the page's code to resolve.
    product = re.search(r'const PRODUCT_NAME = ("[^"]*")', html)
    name = product.group(1) if product else '"mediaMender"'
    return (f"const PRODUCT_NAME = {name};\n"
            f"const BOOT = {json.dumps(boot)};\n{script}")


if __name__ == "__main__":
    text = bundle()
    io.open(sys.argv[1], "w", encoding="utf-8").write(text)
    print(f"extracted {len(text)} chars")
