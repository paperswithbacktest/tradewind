"""Checks on the documentation site that `mkdocs build --strict` does not make by itself.

    python tools/check_docs.py          every script of docs/examples/ is shown on a page
    python tools/check_docs.py --urls   also: every paperswithbacktest.com link of docs/ answers 200

Run by .github/workflows/docs.yml before the build. Standard library only.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EXAMPLES = DOCS / "examples"
PWB_URL = re.compile(r"https://paperswithbacktest\.com[^\s)\"'>`]*")


def unreferenced_examples() -> list[str]:
    """Example scripts that no page includes (``--8<-- "name.py"``) or links to."""
    pages = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.rglob("*.md"))
    missing = []
    for script in sorted(EXAMPLES.glob("*.py")):
        if f'--8<-- "{script.name}"' not in pages and f"examples/{script.name}" not in pages:
            missing.append(script.name)
    return missing


def status(url: str, attempts: int = 3) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "tradewind-docs-check"})
    code = 0
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as err:
            code = err.code
        except (urllib.error.URLError, TimeoutError):
            code = 0
        time.sleep(2 * (attempt + 1))
    return code


def broken_urls() -> list[str]:
    urls = sorted({u.rstrip(".,") for p in DOCS.rglob("*.md")
                   for u in PWB_URL.findall(p.read_text(encoding="utf-8"))})
    bad = []
    for url in urls:
        code = status(url)
        print(f"{code} {url}")
        if code != 200:
            bad.append(f"{code} {url}")
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--urls", action="store_true", help="check the paperswithbacktest.com links")
    args = parser.parse_args(argv)
    errors = [f"docs/examples/{name} is not shown on any page (add it to docs/examples.md)"
              for name in unreferenced_examples()]
    if args.urls:
        errors += [f"link does not answer 200: {line}" for line in broken_urls()]
    for line in errors:
        print(line, file=sys.stderr)
    print("docs checks:", "FAIL" if errors else "ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
