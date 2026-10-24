"""Pack framework: a pack declares ordered steps, the runner executes them idempotently and returns an Envelope.

Rules that keep packs safe to add and re-run:
- Every step may declare `check`: a cheap read-only test that says "already done", so re-running resumes.
- Steps return plain, non-secret outputs. Tokens and passwords live only in memory, never in state or the envelope.
- State is a small 0600 JSON file per (pack, name) under ~/.labagent/packs, used by `destroy` and for display.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..envelope import Envelope


class PackError(Exception):
    """A step failed in a way the learner can act on. The message is shown as-is: keep it free of secrets."""


@dataclass
class Param:
    name: str
    default: Any = None
    help: str = ""
    required: bool = False


@dataclass
class PackContext:
    pack_id: str
    name: str
    params: Dict[str, Any]
    state: Dict[str, Any] = field(default_factory=dict)      # persisted, non-secret
    mem: Dict[str, Any] = field(default_factory=dict)        # in-memory only (tokens, connections)
    services: Dict[str, Any] = field(default_factory=dict)   # injected: dbx, pg_connect, ... (tests swap these)
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)


@dataclass
class Step:
    id: str
    title: str
    run: Callable[[PackContext], Optional[Dict[str, Any]]]
    check: Optional[Callable[[PackContext], bool]] = None


class Pack:
    id: str = ""
    title: str = ""
    summary: str = ""
    aliases: tuple = ()          # short names accepted on the command line and by the natural-language translator
    default_name: str = ""       # instance name used when --name is omitted
    platforms: tuple = ()        # where the pack can run, for the console's platform picker (first is the default)
    cost_tier: str = ""          # free | low | metered. A hint for dashboards, not a price
    params: List[Param] = []

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        raise NotImplementedError

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        """Machine-readable description for `labagent packs --json`. No secrets: defaults are literals from the code."""
        return {"id": self.id, "title": self.title, "summary": self.summary, "aliases": list(self.aliases),
                "default_name": self.default_name, "platforms": list(self.platforms), "cost_tier": self.cost_tier,
                "params": [{"name": q.name, "default": q.default, "help": q.help, "required": q.required} for q in self.params]}

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        """Real service objects for this pack. Tests bypass this by passing their own services."""
        return {}

    def resolve(self, given: Dict[str, Any]) -> Dict[str, Any]:
        known = {p.name for p in self.params}
        unknown = set(given) - known
        if unknown:
            raise PackError(f"unknown parameter(s) for {self.id}: {', '.join(sorted(unknown))}")
        out = {p.name: given.get(p.name, p.default) for p in self.params}
        missing = [p.name for p in self.params if p.required and out[p.name] in (None, "")]
        if missing:
            raise PackError(f"missing required parameter(s): {', '.join(missing)}")
        return out


def state_dir() -> Path:
    return Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "packs"


def state_path(pack_id: str, name: str) -> Path:
    return state_dir() / pack_id / f"{name}.json"


def load_state(pack_id: str, name: str) -> Dict[str, Any]:
    p = state_path(pack_id, name)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(pack_id: str, name: str, state: Dict[str, Any]) -> None:
    p = state_path(pack_id, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))
    p.chmod(0o600)


def run_pack(pack: Pack, action: str, name: str, given: Dict[str, Any], services: Optional[Dict[str, Any]] = None,
             dry_run: bool = False, log: Optional[Callable[[str], None]] = None) -> Envelope:
    env = Envelope(command=f"{action}:{pack.id}", mode="dry-run" if dry_run else "apply", cohort=name)
    try:
        params = pack.resolve(given)
    except PackError as e:
        return env.fail(str(e))
    ctx = PackContext(pack.id, name, params, state=load_state(pack.id, name),
                      services=services if services is not None else pack.services(params.get("profile")))
    if log:
        ctx.log = log
    steps = pack.deploy_steps(ctx) if action == "launch" else pack.destroy_steps(ctx)
    for step in steps:
        item = {"step": step.id, "title": step.title, "state": "planned"}
        env.items.append(item)
        if dry_run:
            continue
        try:
            if step.check and step.check(ctx):
                item["state"] = "skipped"
                continue
            ctx.log(f"[{step.id}] {step.title}")
            out = step.run(ctx) or {}
            ctx.state.update(out)
            save_state(pack.id, name, ctx.state)
            item["state"] = "done"
        except PackError as e:
            item.update(state="failed", error=str(e)[:400])
            break
        except Exception as e:  # unexpected: class name only, never the message (could carry a token)
            item.update(state="failed", error=type(e).__name__)
            break
    if dry_run:
        env.summary = {"steps": len(env.items)}
        return env
    env.summary = {s: sum(1 for i in env.items if i["state"] == s) for s in ("done", "skipped", "failed")}
    failed = [i for i in env.items if i["state"] == "failed"]
    if failed:
        env.fail(f"step '{failed[0]['step']}' failed: {failed[0].get('error', '')}")
    else:
        env.summary["outputs"] = {k: v for k, v in ctx.state.items() if k in getattr(pack, "output_keys", ())}
        if action == "destroy":
            state_path(pack.id, name).unlink(missing_ok=True)
    return env
