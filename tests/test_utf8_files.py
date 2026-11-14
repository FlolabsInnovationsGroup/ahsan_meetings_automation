"""Every text file the render code reads or writes names UTF-8. Without it Windows uses cp1252 and an
ellipsis or a name with an accent turns into garbage in the burned-in captions."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [*sorted((ROOT / "slice").glob("*.py")), ROOT / "outputs.py", ROOT / "pipeline.py", ROOT / "deliver.py",
         ROOT / "decide" / "topics.py", ROOT / "edl" / "builder.py", ROOT / "analytics" / "suggest.py"]
CALL = re.compile(r"\.(write_text|read_text)\(")


def _call_text(src: str, start: int) -> str:
    depth, i = 0, start
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    return src[start:]


def test_text_files_are_read_and_written_as_utf8():
    missing = []
    for path in FILES:
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        for m in CALL.finditer(src):
            if "encoding=" not in _call_text(src, m.end() - 1):
                missing.append(f"{path.name}:{src.count(chr(10), 0, m.start()) + 1}")
    assert not missing, missing
