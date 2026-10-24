"""Natural language to labagent command, via any OpenAI-compatible chat endpoint (default: local Ollama).

The model never runs anything. Its reply is parsed as JSON, checked against a closed set of actions, real pack ids and
declared parameters, and every name or value must appear in the user's own sentence (so nothing can be invented).
The result is shown as a normal command line, and the caller asks for confirmation before running it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from . import __version__

DEFAULT_URL = "http://localhost:11434/v1"      # Ollama's OpenAI-compatible endpoint
DEFAULT_MODEL = "qwen2.5-coder:0.5b"           # about 400 MB: runs on any laptop
ACTIONS = ("launch", "destroy", "list")
DESTROY_WORDS = re.compile(r"\b(delete|destroy|tear\s*down|teardown|remove|get\s+rid\s+of|clean\s*up|wipe|drop|terminate|kill)\b", re.I)
LAUNCH_WORDS = re.compile(r"\b(launch|run|deploy|start|create|build|set\s*up|setup|spin\s*up|install|provision|bring\s*up)\b", re.I)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class NLError(Exception):
    """Something the user can act on (endpoint down, ambiguous request, rejected model output)."""


@dataclass
class Plan:
    action: str
    pack: Optional[str] = None
    name: Optional[str] = None
    params: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def argv(self) -> List[str]:
        if self.action == "list":
            return ["packs"]
        out = [self.action, self.pack or "", "--name", self.name or ""]
        for k, v in self.params.items():
            out += ["--param", f"{k}={v}"]
        return out

    def command_line(self) -> str:
        return "labagent " + " ".join(shlex.quote(a) for a in self.argv())


# -- prompt --------------------------------------------------------------------
def build_messages(text: str, packs: Dict[str, Any]) -> List[Dict[str, str]]:
    """A deliberately small task for a small model: pick the action and the pack. Names and parameters are not the
    model's job (they are read from the sentence by exact rules in `extract_args`), so it cannot invent them."""
    plist = sorted(packs.values(), key=lambda x: x.id)
    lines = [f"- {p.id}" + (f" (alias: {', '.join(p.aliases)})" if p.aliases else "") + f": {p.summary.split('.')[0]}" for p in plist]
    rag = next((p.id for p in plist if "tutorial" in p.id), plist[0].id)       # what a vague "the rag app" request most likely means
    shots = []
    for p in plist:
        short = p.aliases[0] if p.aliases else p.id
        shots += [(f"deploy {short}", "launch", p.id), (f"delete {p.id}", "destroy", p.id)]
    shots += [("run and start the rag app on databricks", "launch", rag), ("what can you deploy?", "list", None),
              ("show me the available packs", "list", None), ("tear down everything for the sec rag", "destroy", rag)]
    ex = "\n".join(f"Request: {q}\n" + json.dumps({"action": a, "pack": k}) for q, a, k in shots)
    system = ("Convert the request to JSON: {\"action\": ..., \"pack\": ...}. Reply with JSON only.\n"
              "action is launch (run, deploy, start, create, build), destroy (delete, remove, tear down) or list (show what exists). "
              "pack is one of the ids below, or null for list.\n\nPacks:\n" + "\n".join(lines) + "\n\nExamples:\n" + ex)
    return [{"role": "system", "content": system}, {"role": "user", "content": f"Request: {text}"}]


STOP = {"a", "an", "the", "to", "and", "in", "on", "with", "of", "for", "it", "my", "as", "is"}


def extract_args(text: str, pack: Any) -> Dict[str, Any]:
    """Read `called <name>` and `<param> <value>` straight from the sentence. Exact rules, no model involved."""
    out: Dict[str, Any] = {"name": None, "params": {}}
    m = re.search(r"\b(?:called|named)\s+[\"']?([A-Za-z0-9][A-Za-z0-9-]{0,40})", text, re.I)
    if m and m.group(1).lower() not in STOP and NAME_RE.match(m.group(1).lower()):
        out["name"] = m.group(1).lower()
    for prm in pack.params:
        m = re.search(rf"(?<![\w-]){re.escape(prm.name)}(?![\w-])\s*(?:=|:|\bis\b|\bto\b)?\s*[\"']?([^\s\"',;]+(?:,[^\s\"',;]+)*)", text, re.I)
        if m and m.group(1).lower() not in STOP:
            out["params"][prm.name] = m.group(1)
    return out


def literal_pack(text: str, packs: Dict[str, Any]) -> Optional[Any]:
    """The pack the user named outright (id or alias, longest match wins), if any."""
    low, best = text.lower(), None
    for p in packs.values():
        for tok in (p.id, *p.aliases):
            if re.search(rf"(?<![\w-]){re.escape(tok.lower())}(?![\w-])", low) and (best is None or len(tok) > best[0]):
                best = (len(tok), p)
    return best[1] if best else None


# -- model call ----------------------------------------------------------------
def call_llm(url: str, model: str, messages: List[Dict[str, str]], api_key: Optional[str] = None,
             timeout: int = 180, opener: Callable = urllib.request.urlopen) -> str:
    endpoint = url.rstrip("/") + "/chat/completions"
    host = urlparse(url).hostname or ""
    headers = {"Content-Type": "application/json", "User-Agent": f"labagent/{__version__}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {"model": model, "messages": messages, "temperature": 0, "stream": False, "response_format": {"type": "json_object"}}
    for attempt in (0, 1):
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with opener(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 400 and attempt == 0 and "response_format" in body:
                body.pop("response_format")           # some servers reject JSON mode: retry without it
                continue
            if e.code == 404 or "not found" in detail.lower():
                raise NLError(f"model '{model}' is not available at {host}. Pull it with: ollama pull {model}") from e
            raise NLError(f"the model endpoint returned HTTP {e.code}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise NLError(f"cannot reach the model endpoint at {url}. For the default local setup run: "
                          f"ollama serve  and  ollama pull {DEFAULT_MODEL}  (or set LABAGENT_LLM_URL / --endpoint)") from e
        except (KeyError, IndexError, ValueError) as e:
            raise NLError("the model endpoint returned an unexpected response") from e
    raise NLError("the model endpoint did not accept the request")


# -- validation ----------------------------------------------------------------
def _extract_json(raw: str) -> Dict[str, Any]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        i = raw.find("{")
        depth = 0
        obj = None
        for j in range(max(i, 0), len(raw)):
            depth += (raw[j] == "{") - (raw[j] == "}")
            if i >= 0 and depth == 0:
                try:
                    obj = json.loads(raw[i:j + 1])
                except ValueError:
                    pass
                break
    if not isinstance(obj, dict):
        raise NLError("the model did not return a usable command. Try rephrasing, or use the explicit form: labagent launch <pack>")
    return obj


def _canonical_action(model_action: str, text: str) -> str:
    """Map the model's word (it may say 'create' or 'deploy') to launch/destroy/list. If it gave something unusable, the
    verbs in the sentence decide, but only when they point one way."""
    a = model_action.lower().strip()
    if a in ACTIONS:
        return a
    if LAUNCH_WORDS.fullmatch(a):
        return "launch"
    if DESTROY_WORDS.fullmatch(a):
        return "destroy"
    if re.search(r"\b(list|show|available|what can)\b", text, re.I) and not LAUNCH_WORDS.search(text) and not DESTROY_WORDS.search(text):
        return "list"
    d, l = bool(DESTROY_WORDS.search(text)), bool(LAUNCH_WORDS.search(text))
    return "destroy" if d and not l else "launch" if l and not d else a


GENERIC = {"rag", "app", "the", "and", "for", "from", "over", "pack"}


def _distinctive_tokens(pack: Any, packs: Dict[str, Any]) -> set:
    """Words that point at this pack: in its id, aliases or title, shared with at most one other pack (a snapshot/live pair
    is fine) and not generic. Stays correct as packs are added, unlike 'not shared by all packs'."""
    def toks(p):
        return {t for tok in (p.id, *p.aliases, p.title) for t in re.split(r"[^a-z0-9]+", tok.lower()) if len(t) > 2}
    owners: Dict[str, int] = {}
    for p in packs.values():
        for t in toks(p):
            owners[t] = owners.get(t, 0) + 1
    return {t for t in toks(pack) if owners[t] <= 2 and t not in GENERIC}


def validate(raw: str, text: str, resolve: Callable[[str], Any], packs: Optional[Dict[str, Any]] = None) -> Plan:
    """Turn model output into a Plan or refuse. `resolve` maps a pack token to a Pack (raises KeyError)."""
    obj = _extract_json(raw)
    action = _canonical_action(str(obj.get("action") or ""), text)
    if action not in ACTIONS:
        raise NLError(f"I could not tell what to do (got '{action or 'nothing'}'). Say launch, destroy or list, e.g. "
                      "\"deploy rag-tutorial\"")
    wants_destroy, wants_launch = bool(DESTROY_WORDS.search(text)), bool(LAUNCH_WORDS.search(text))
    if wants_destroy and wants_launch:
        raise NLError("that asks for two things at once (create and delete). One action at a time: run them as two commands")
    if action == "destroy" and not wants_destroy:
        raise NLError("the model chose 'destroy' but your request does not ask to delete anything. Nothing was run")
    if action == "launch" and wants_destroy:
        raise NLError("your request sounds like a deletion but the model chose 'launch'. Nothing was run: "
                      "say it explicitly, e.g. \"destroy rag-tutorial\"")
    if action == "list":
        return Plan("list")
    pack, warnings = None, []
    named = literal_pack(text, packs) if packs else None
    token = obj.get("pack")
    if named is not None:                       # the user typed a pack id or alias: that beats the model's guess
        pack = named
        if token and str(token).lower() not in [named.id, *[a.lower() for a in named.aliases]]:
            warnings.append(f"used '{named.id}' because you named it")
    elif token:
        try:
            pack = resolve(str(token))
        except KeyError as e:
            raise NLError(str(e).strip("\"'")) from e
        words = set(re.split(r"[^a-z0-9]+", text.lower()))
        if packs and not (_distinctive_tokens(pack, packs) & words):
            raise NLError("I could not tell which pack you mean (the model guessed). Name it, e.g. \"deploy rag-tutorial\", "
                          "or run 'labagent packs'")
    if pack is None:
        raise NLError("which pack? Run 'labagent packs' to see them, e.g. \"deploy rag-tutorial\"")
    args = extract_args(text, pack)
    plan = Plan(action, pack.id, args["name"] or pack.default_name or None, args["params"], warnings)
    if not plan.name:
        raise NLError(f"{pack.id} needs an instance name: add 'called <name>' to your request")
    return plan


def translate(text: str, packs: Dict[str, Any], resolve: Callable[[str], Any], url: str = DEFAULT_URL,
              model: str = DEFAULT_MODEL, api_key: Optional[str] = None,
              chat: Optional[Callable[..., str]] = None) -> Plan:
    text = " ".join(text.split())
    if not text:
        raise NLError("say what you want, e.g. labagent \"deploy rag-tutorial on databricks\"")
    chat = chat or call_llm          # looked up at call time so callers and tests can substitute it
    return validate(chat(url, model, build_messages(text, packs), api_key), text, resolve, packs)


def settings(endpoint: Optional[str], model: Optional[str]) -> Dict[str, Optional[str]]:
    return {"url": endpoint or os.environ.get("LABAGENT_LLM_URL") or DEFAULT_URL,
            "model": model or os.environ.get("LABAGENT_LLM_MODEL") or DEFAULT_MODEL,
            "api_key": os.environ.get("LABAGENT_LLM_API_KEY")}


def is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")
