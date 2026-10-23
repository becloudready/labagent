"""rag-ide: give each learner a private browser VS Code with the RAG lab, on serverless Modal. No learner sign-up, nothing on their laptop.

The instructor's Modal login stays on the instructor's machine. Each learner gets a URL and a random password (written to a
0600 credentials file, never to state or logs), a persistent volume at work/, and an idle timeout. See docs for the cost model.
"""
from __future__ import annotations

import csv
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step, state_dir
from .modal_gateway import ModalGateway, app_name, slug

MAX_LEARNERS = 60


def learner_names(spec: Any) -> List[str]:
    """'3' -> learner-01..03; 'ana,ben' -> ana, ben. Names become resource names, so they are slugged and must be unique."""
    text = str(spec).strip()
    if re.fullmatch(r"\d+", text):
        n = int(text)
        names = [f"learner-{i:02d}" for i in range(1, n + 1)]
    else:
        names = [slug(p) for p in text.split(",") if p.strip()]
    if not names:
        raise PackError("learners must be a count (e.g. 5) or a comma list of names (e.g. ana,ben)")
    if len(set(names)) != len(names):
        raise PackError("learner names must be unique after normalising")
    if len(names) > MAX_LEARNERS:
        raise PackError(f"at most {MAX_LEARNERS} learners per cohort in one launch")
    return names


def checkout_root() -> str:
    root = Path(__file__).resolve().parents[3]
    return str(root) if (root / "pyproject.toml").is_file() and (root / "labagent" / "ai" / "rag").is_dir() else ""


def creds_path(name: str) -> Path:
    return state_dir() / "rag-ide" / f"{name}-credentials.csv"


class _LazyModal:
    """Creates the real gateway on first use, inside a step, so a missing `modal` package is a clean step failure
    (and `--dry-run` works without it) instead of a crash while the pack is being set up."""

    def __init__(self) -> None:
        self._gw = None

    def __getattr__(self, name: str):
        if self._gw is None:
            self._gw = ModalGateway()
        return getattr(self._gw, name)


class RagIde(Pack):
    id = "rag-ide"
    title = "Browser VS Code lab per learner (Modal)"
    summary = "Serverless web IDE with the RAG lab for each learner: one Modal login, nothing for learners to install"
    aliases = ("ide", "web-ide", "rag-web-ide")
    default_name = "cohort"
    params = [
        Param("learners", "1", "Count (5) or comma list of names (ana,ben)"),
        Param("hours", 4, "Hard cap per session, 1 to 24"),
        Param("idle_minutes", 15, "Shut a session down after this long with no browser connected"),
        Param("cpu", 2.0, "CPU cores per learner"),
        Param("memory_mib", 4096, "Memory per learner"),
        Param("image", "auto", "auto | pypi | path to a labagent checkout"),
        Param("llm", "none", "none (mock answers) | groq-shared (copies GROQ_API_KEY from your shell into each learner's IDE)"),
        Param("groq_model", "openai/gpt-oss-20b", "Used when llm=groq-shared"),
        Param("show_passwords", "auto", "auto (print them for 3 learners or fewer) | yes | no. They are always in the credentials file"),
        Param("keep_work", False, "destroy: keep the learners' volumes"),
    ]

    def services(self, profile):
        return {"modal": _LazyModal()}

    def _validate(self, ctx: PackContext) -> None:
        p = ctx.params
        names = learner_names(p["learners"])
        if not 1 <= int(p["hours"]) <= 24:
            raise PackError("hours must be between 1 and 24")
        if p["show_passwords"] not in ("auto", "yes", "no"):
            raise PackError("show_passwords must be auto, yes or no")
        if p["llm"] not in ("none", "groq-shared"):
            raise PackError("llm must be 'none' or 'groq-shared'")
        if p["llm"] == "groq-shared" and not os.environ.get("GROQ_API_KEY"):
            raise PackError("llm=groq-shared needs GROQ_API_KEY in your shell. The key is visible to every learner: use a throwaway key with a low limit.")
        ctx.mem["names"] = names

    def _preflight(self, ctx: PackContext):
        self._validate(ctx)
        workspace = ctx.services["modal"].check_auth()
        src = ctx.params["image"]
        if src == "auto":
            src = checkout_root() or "pypi"
        elif src != "pypi" and not (Path(src) / "pyproject.toml").is_file():
            raise PackError(f"image path '{src}' is not a labagent checkout")
        ctx.log(f"  Modal workspace: {workspace} | image source: {src} | learners: {len(ctx.mem['names'])}")
        return {"workspace": workspace, "image_source": src, "app": app_name(ctx.name)}

    def _build(self, ctx: PackContext):
        gw = ctx.services["modal"]
        app = gw.app(ctx.state["app"])
        ctx.mem["app"] = app
        ctx.mem["image"] = gw.image(ctx.state["image_source"])
        gw.build(app, ctx.mem["image"])       # first build takes several minutes; later runs hit Modal's cache
        return {"image_built": True}

    def _learners(self, ctx: PackContext):
        gw = ctx.services["modal"]
        p = ctx.params
        names = ctx.mem.get("names") or learner_names(p["learners"])
        learners: Dict[str, Dict[str, str]] = dict(ctx.state.get("learners", {}))
        passwords: Dict[str, str] = {}
        env = {"GROQ_API_KEY": os.environ["GROQ_API_KEY"], "GROQ_MODEL": p["groq_model"], "LLM_PROVIDER": "groq"} if p["llm"] == "groq-shared" else {}
        app = ctx.mem.get("app") or gw.app(ctx.state["app"])
        image = ctx.mem.get("image") or gw.image(ctx.state["image_source"])
        for n in names:
            if n in learners and gw.is_alive(learners[n]["sandbox_id"]):
                ctx.log(f"  {n}: already running")
                continue
            pw = secrets.token_urlsafe(12)
            ctx.log(f"  starting {n} ...")
            learners[n] = gw.launch(app, ctx.name, n, pw, image, float(p["cpu"]), int(p["memory_mib"]), int(p["hours"]) * 3600, int(p["idle_minutes"]) * 60, env)
            passwords[n] = pw
            ctx.state["learners"] = learners        # persist progress so a failure part-way resumes
        ctx.mem["passwords"] = passwords
        return {"learners": learners}

    def _credentials(self, ctx: PackContext):
        path = creds_path(ctx.name)
        old: Dict[str, str] = {}
        if path.exists():
            old = {r["learner"]: r["password"] for r in csv.DictReader(path.open())}
        pw = {**old, **ctx.mem.get("passwords", {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["learner", "url", "password"])
            for n, info in sorted(ctx.state["learners"].items()):
                w.writerow([n, info["url"], pw.get(n, "(lost: destroy and relaunch this learner)")])
        learners = sorted(ctx.state["learners"].items())
        sp = ctx.params["show_passwords"]
        show = sp == "yes" or (sp == "auto" and len(learners) <= 3)
        ctx.log("\n  Open each URL in a browser and sign in with the password:")
        for n, info in learners:
            ctx.log(f"    {n}: {info['url']}" + (f"   password: {pw.get(n, '(lost: destroy and relaunch)')}" if show else ""))
        ctx.log(f"\n  Passwords are in {path} (0600, one row per learner)" + ("" if show else f"\n  View them: cat {path}"))
        return {"credentials_file": str(path)}

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [
            Step("preflight", "Check the Modal login and settings", self._preflight),
            Step("image", "Build the lab image (first time: several minutes)", self._build),
            Step("learners", "Start one private IDE per learner", self._learners),
            Step("credentials", "Write the credentials file", self._credentials),
        ]

    def _stop_all(self, ctx: PackContext):
        gw = ctx.services["modal"]
        ids = {i["sandbox_id"] for i in ctx.state.get("learners", {}).values()} | set(gw.find(ctx.name))
        for sid in ids:
            gw.stop(sid)
        return {"learners": {}, "stopped": len(ids)}

    def _volumes(self, ctx: PackContext):
        if ctx.params["keep_work"]:
            return {}
        gw = ctx.services["modal"]
        for vol in ctx.state.get("volumes", []):
            gw.delete_volume(vol)
        return {"volumes": []}

    def _forget(self, ctx: PackContext):
        creds_path(ctx.name).unlink(missing_ok=True)
        return {"credentials_file": None}

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        # remember volume names before the learner records are cleared
        ctx.state["volumes"] = sorted({i["volume"] for i in ctx.state.get("learners", {}).values() if i.get("volume")} | set(ctx.state.get("volumes", [])))
        return [
            Step("preflight", "Check the Modal login", lambda c: {"workspace": c.services["modal"].check_auth()}),
            Step("stop", "Stop every learner's IDE", self._stop_all),
            Step("volumes", "Delete learner volumes (unless keep_work)", self._volumes),
            Step("credentials", "Remove the credentials file", self._forget),
        ]
