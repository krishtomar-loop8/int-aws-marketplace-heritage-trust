"""Render installer.yaml by inlining the Lambda handler source.

Usage: python -m installer.render <out_path>
"""

import re
import sys
from pathlib import Path

PLACEHOLDER = "#HANDLER_SOURCE#"
HERE = Path(__file__).resolve().parent


def render(template_path: Path, handler_path: Path) -> str:
    template = Path(template_path).read_text()
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"{PLACEHOLDER} must appear exactly once in {template_path}")
    match = re.search(rf"^([ \t]*){re.escape(PLACEHOLDER)}[ \t]*$", template, re.MULTILINE)
    if not match:
        raise ValueError(f"{PLACEHOLDER} must be alone on its own line")
    indent = match.group(1)
    source = Path(handler_path).read_text()
    body = "\n".join((indent + line) if line else "" for line in source.split("\n"))
    # Strip the trailing newline of the placeholder line's replacement so that the
    # block scalar keeps the source's own final newline (clip chomping).
    return template[: match.start()] + body.rstrip("\n") + template[match.end() :]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m installer.render <out_path>", file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(HERE / "installer.yaml", HERE / "function" / "handler.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
