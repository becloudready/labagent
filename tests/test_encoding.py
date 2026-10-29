"""Text files and subprocess output must be read and written as UTF-8.

On Windows the default encoding is cp1252, so a bare write_text() of the AML corpus
(which has characters such as the section sign and curly quotes) raised
UnicodeEncodeError and the launch stopped at the corpus step. Linux and macOS use
UTF-8 by default, so CI cannot see this; this test reads the source instead.
"""
import ast
import pathlib

import labagent

ROOT = pathlib.Path(labagent.__file__).parent
SKIP_RECEIVERS = {"os", "webbrowser", "urllib", "request"}


def _problems(path):
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        kw = {k.arg for k in n.keywords}
        f = n.func
        what = None
        if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
            what = f.attr
        elif isinstance(f, ast.Name) and f.id == "open":
            what = "open"
        elif isinstance(f, ast.Attribute) and f.attr == "open" and not (isinstance(f.value, ast.Name) and f.value.id in SKIP_RECEIVERS):
            what = "open"
        if what == "open":
            args = list(n.args) if isinstance(f, ast.Attribute) else list(n.args[1:])
            mode = args[0] if args else next((k.value for k in n.keywords if k.arg == "mode"), None)
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                continue
        if what and "encoding" not in kw:
            out.append(f"{path.relative_to(ROOT)}:{n.lineno} {what}() without encoding")
        text_true = any(k.arg == "text" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)
        if text_true and "encoding" not in kw:
            out.append(f"{path.relative_to(ROOT)}:{n.lineno} text=True without encoding")
    return out


def test_every_text_file_and_subprocess_call_names_its_encoding():
    found = [p for f in sorted(ROOT.rglob("*.py")) for p in _problems(f)]
    assert not found, "Add encoding=\"utf-8\":\n" + "\n".join(found)
