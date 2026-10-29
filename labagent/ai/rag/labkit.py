"""Small helpers for the step-by-step RAG lab notebooks (labagent/ai/rag/lab).

Everything the lab is *about* (chunking, embedding, searching, the prompt) is written by the learner. This module holds the
parts that are not the lesson: reading the sample documents, talking to an LLM over HTTP, and the self-check messages.
It is short on purpose, so a learner can read it.
"""
import os
import urllib.error
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from .config import GROQ_URL, OLLAMA_URL, OPENAI_URL, load_dotenv
from .llm import http_json

def lab_root() -> Path:
    """The lab folder: the notebooks run inside notebooks/, so its parent, or the current folder when run from the top."""
    cwd = Path.cwd()
    return cwd.parent if cwd.name == "notebooks" else cwd


def load_env() -> None:
    """Read .env from the lab folder so GROQ_API_KEY and friends reach this process."""
    load_dotenv(lab_root() / ".env")


def load_docs(folder: Optional[str] = None) -> Dict[str, str]:
    """name -> text for every .md and .txt file in the folder (default: data/sample_docs), plus the PDF if pypdf is installed."""
    base = Path(folder) if folder else lab_root() / "data" / "sample_docs"
    docs: Dict[str, str] = {}
    for f in sorted(base.iterdir()):
        if f.suffix in (".md", ".txt"):
            docs[f.stem] = f.read_text(encoding="utf-8")
        elif f.suffix == ".pdf":
            try:
                from pypdf import PdfReader
                docs[f.stem] = "\n".join(p.extract_text() or "" for p in PdfReader(str(f)).pages)
            except ImportError:
                print(f"skipping {f.name}: pip install pypdf to read PDFs")
    return docs


class Check:
    """Self-checks. `check('name', condition, 'hint')` prints a tick, or stops the notebook with a hint."""

    passed = 0

    def __call__(self, name: str, ok: bool, hint: str = "") -> None:
        if not ok:
            raise AssertionError(f"Check failed: {name}. {hint}".strip())
        Check.passed += 1
        print(f"  ok  {name}")


check = Check()


class Reply(NamedTuple):
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int


def _ollama_up() -> bool:
    try:
        http_json(os.environ.get("OLLAMA_URL", OLLAMA_URL).rstrip("/") + "/models", {}, {}, 1.5)
        return True
    except Exception:  # noqa: BLE001 (any failure means: not running)
        return False


def chat(system: str, user: str, temperature: float = 0.1) -> Reply:
    """Send one system + user message to an LLM and return the reply.

    Picks Groq if GROQ_API_KEY is set, then OpenAI, then a local Ollama, and otherwise answers with a clearly marked mock
    so every notebook still runs with no key. Set LLM_PROVIDER=mock to force the mock.
    """
    forced = (os.environ.get("LLM_PROVIDER") or "").lower()
    choices = {
        "groq": (GROQ_URL, os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_MODEL") or "openai/gpt-oss-20b"),
        "openai": (OPENAI_URL, os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"),
        "ollama": (os.environ.get("OLLAMA_URL", OLLAMA_URL).rstrip("/"), "ollama", os.environ.get("OLLAMA_MODEL") or "qwen2.5-coder:0.5b"),
    }
    order = [forced] if forced and forced != "mock" else ([] if forced == "mock" else [n for n in ("groq", "openai") if choices[n][1]] + (["ollama"] if _ollama_up() else []))
    for name in order:
        url, key, model = choices[name]
        try:
            r = http_json(url + "/chat/completions", {"Authorization": f"Bearer {key}"},
                          {"model": model, "temperature": temperature, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, 60)
            u = r.get("usage") or {}
            return Reply(r["choices"][0]["message"]["content"].strip(), name, model, u.get("prompt_tokens", len(user) // 4), u.get("completion_tokens", 0))
        except urllib.error.HTTPError as e:
            print(f"[{name}] HTTP {e.code}: trying the next option" + (" (set GROQ_MODEL to a model your account has)" if e.code == 404 else ""))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f"[{name}] failed ({type(e).__name__}): trying the next option")
    first = user.split("\n\n")[0][:300].replace("\n", " ")
    text = "[mock answer: no LLM is configured. Set GROQ_API_KEY in .env for real answers] The prompt you sent starts with: " + first
    return Reply(text, "mock", "none", len(system + user) // 4, len(text) // 4)


def words(text: str) -> List[str]:
    """Lower-case word list with punctuation removed. Used by the keyword exercises."""
    import re
    return re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower())
