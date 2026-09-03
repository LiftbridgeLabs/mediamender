"""Extract the rendered inline JavaScript so node can parse and exercise it."""
import io
import re
import sys

sys.path.insert(0, ".")
import app

html = app.app.test_client().get("/").get_data(as_text=True)
js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
# The init block, which registers the hashchange listener, is skipped until a
# config file exists. The harness needs it to run.
js = js.replace("const _configMissing = true;", "const _configMissing = false;")
io.open(sys.argv[1], "w", encoding="utf-8").write(js)
print(f"extracted {len(js)} chars")
