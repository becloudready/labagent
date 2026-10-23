"""LLM gateway: Groq, Ollama, OpenAI and Anthropic over plain HTTPS, with a graceful fallback to a mock answer.

Order when LLM_PROVIDER is unset: Groq (if GROQ_API_KEY), OpenAI, Anthropic, local Ollama (if it answers), then mock.
A provider that fails at call time falls through to the next one and the reason is returned as a warning.
API keys are only ever sent to their own provider and are never logged.
"""
import json
import urllib.error
import urllib.request
from typing import Callable, List, Optional

from pydantic import BaseModel

from .config import ANTHROPIC_URL, GROQ_URL, OPENAI_URL, RAGSettings

SYSTEM_PROMPT = ("Answer the question using only the context. Cite sources as [doc_id]. "
                 "If the context does not contain the answer, say you do not know.")

Transport = Callable[[str, dict, dict, float], dict]


class LLMResult(BaseModel):
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False
    warnings: List[str] = []


def http_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    # An explicit User-Agent is required: Groq's gateway answers Python's default one with 403 "error code: 1010".
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "labagent-rag/0.2 (+https://github.com/becloudready/labagent)", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https endpoints or the local Ollama URL)
        return json.loads(r.read())


def _est(text: str) -> int:
    return max(1, len(text) // 4)


class LLMGateway:
    def __init__(self, settings: RAGSettings, transport: Optional[Transport] = None):
        self.s = settings
        self._post = transport or http_json

    def _candidates(self) -> List[str]:
        s = self.s
        if s.llm_provider:
            return [s.llm_provider.lower(), "mock"]
        order = [name for name, key in (("groq", s.groq_api_key), ("openai", s.openai_api_key), ("anthropic", s.anthropic_api_key)) if key]
        return order + ["ollama", "mock"]

    def complete(self, question: str, context: str) -> LLMResult:
        warnings: List[str] = []
        user = f"Context:\n{context}\n\nQuestion: {question}"
        for provider in self._candidates():
            try:
                res = self._call(provider, user, question, context)
                res.warnings = warnings
                return res
            except _Unavailable as e:
                warnings.append(str(e))
            except urllib.error.HTTPError as e:
                hint = {401: "check the API key", 403: "request blocked", 404: "model not available to this key: set GROQ_MODEL, list yours at /openai/v1/models", 429: "rate limit reached"}.get(e.code, "")
                warnings.append(f"{provider} failed: HTTP {e.code}{' (' + hint + ')' if hint else ''}; trying the next provider")
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                warnings.append(f"{provider} failed ({type(e).__name__}); trying the next provider")
        raise RuntimeError("unreachable: mock never fails")

    def _call(self, provider: str, user: str, question: str, context: str) -> LLMResult:
        s = self.s
        if provider == "mock":
            first = context.split("\n\n")[0] if context else "no context retrieved"
            text = ("[mock answer: no LLM provider is configured] Most relevant passage:\n" + first
                    + "\n\nFix: set GROQ_API_KEY (free tier at console.groq.com) or run `ollama serve`.")
            return LLMResult(text=text, provider="mock", model="none", prompt_tokens=_est(user), completion_tokens=_est(text), estimated=True)
        if provider == "anthropic":
            if not s.anthropic_api_key:
                raise _Unavailable("anthropic selected but ANTHROPIC_API_KEY is not set")
            d = self._post(f"{ANTHROPIC_URL}/messages", {"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"},
                           {"model": s.anthropic_model, "max_tokens": 600, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": user}]}, s.llm_timeout_s)
            text = "".join(b.get("text", "") for b in d["content"])
            u = d.get("usage") or {}
            return LLMResult(text=text, provider=provider, model=s.anthropic_model, prompt_tokens=u.get("input_tokens") or _est(user),
                             completion_tokens=u.get("output_tokens") or _est(text), estimated=not u)
        url, key, model = {
            "groq": (GROQ_URL, s.groq_api_key, s.groq_model),
            "openai": (OPENAI_URL, s.openai_api_key, s.openai_model),
            "ollama": (s.ollama_url.rstrip("/"), "ollama", s.ollama_model),
        }.get(provider, (None, None, None))
        if url is None:
            raise _Unavailable(f"unknown LLM_PROVIDER '{provider}': use groq, ollama, openai, anthropic or mock")
        if not key:
            raise _Unavailable(f"{provider} selected but its API key is not set")
        d = self._post(f"{url}/chat/completions", {"Authorization": f"Bearer {key}"},
                       {"model": model, "temperature": 0.1, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]}, s.llm_timeout_s)
        text = d["choices"][0]["message"]["content"]
        u = d.get("usage") or {}
        return LLMResult(text=text, provider=provider, model=model, prompt_tokens=u.get("prompt_tokens") or _est(user),
                         completion_tokens=u.get("completion_tokens") or _est(text), estimated=not u)


class _Unavailable(Exception):
    pass
