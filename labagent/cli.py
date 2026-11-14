"""labagent command line. stdout is silent unless --output - is given; logs go to stderr (ids and counts only)."""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

import boto3

from . import __version__
from .config import STATE_BACKEND_DIR, load_config
from .context import Ctx, GuardError
from .envelope import Envelope
from .run import COMMANDS, PACK_COMMANDS, run_job, run_pack_job


def _emit(env: Envelope, output: str) -> None:
    text = json.dumps(env.to_dict(), indent=2)
    if output == "-":
        print(text)
    else:
        Path(output).write_text(text, encoding="utf-8")
        Path(output).chmod(0o600)


def _packs_main(args) -> int:
    from .packs import registry
    from .packs.base import run_pack
    if args.cmd == "packs":
        if args.json:
            print(json.dumps({"packs": [pk.describe() for _, pk in sorted(registry.all_packs().items())]}, indent=2))
            return 0
        for pid, pk in sorted(registry.all_packs().items()):
            print(f"{pid:16} {pk.summary}")
        return 0
    from . import spec as spec_mod
    try:
        spec = spec_mod.load(args.file) if args.file else {"pack": None, "name": None, "params": {}}
        token = args.pack or spec["pack"]
        if not token:
            print("error: give a pack name, or a launch file with -f", file=sys.stderr)
            return 2
        pack = registry.resolve(token)
        if args.pack and spec["pack"] and registry.resolve(spec["pack"]).id != pack.id:
            print(f"error: {args.file} is for the pack '{spec['pack']}', not '{args.pack}'", file=sys.stderr)
            return 2
        given = {**spec["params"], **dict(kv.split("=", 1) for kv in args.param)}       # flags win over the file
    except spec_mod.SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    name = args.name or spec["name"] or pack.default_name
    if not name:
        print("error: --name is required for this pack", file=sys.stderr)
        return 2
    from .console import Console
    console = Console(color=args.color)                   # progress goes to stderr; styled only for a person at a terminal
    pretty = console.color
    if pretty:
        console.banner(pack.id, args.cmd, name, args.dry_run)
    try:
        env = run_pack(pack, args.cmd, name, given, dry_run=args.dry_run, log=console.log if pretty else None, ui=console if pretty else None)
    finally:
        console.close()
    output = args.output
    if output is None and (args.json or not (pretty and sys.stdout.isatty())):
        output = "-"                    # a pipe, a file or --json gets the result document, exactly as before
    if output is not None:
        _emit(env, output)
    if pretty:
        _finish(console, env, pack.id, args.cmd, name, args.dry_run, shown_json=output is not None)
    return env.exit_code


def _finish(console, env, pack_id: str, action: str, name: str, dry_run: bool, shown_json: bool) -> None:
    """The closing lines of a styled run: what to do next, and how to get the result document."""
    s = console.s
    if env.status != "ok":
        failed = next((i for i in env.items if i.get("state") == "failed"), {})
        text = (env.error or "failed")
        lines = [s(f"step {failed.get('step', '?')} failed", "red", "bold")] + [s(t, "red") for t in textwrap.wrap(text, max(console.width - 6, 30))]
        lines += ["", s("Run the same command again to resume where it stopped, or remove what was created:", "gray"),
                  s(f"labagent destroy {pack_id} --name {name}", "cyan")]
        console.card("Failed", lines, "red")
    elif dry_run:
        console.line()
        console.line(" " + s("Nothing was created. Run it for real: leave out --dry-run.", "gray"))
    elif action == "destroy":
        console.line()
        console.line(" " + s(console.sym["ok"], "green", "bold") + " " + s(f"{pack_id} {name} removed", "bold"))
    if not shown_json:
        console.line(" " + s("The result as JSON: add --json, or --output FILE.", "gray"))


KNOWN = {"run", "packs", "rag", "dataset", "launch", "destroy", "offers", "ssh-keys", "bootstrap-path", "commands"}


def _nl_main(argv) -> int:
    """labagent "deploy rag-tutorial on databricks": translate with a small model, confirm, then run the real command."""
    from . import nl
    from .packs import registry
    q = argparse.ArgumentParser(prog="labagent", description="Plain-English front end: labagent \"deploy rag-tutorial on databricks\"")
    q.add_argument("text", nargs="+")
    q.add_argument("--endpoint", help=f"OpenAI-compatible base URL (default {nl.DEFAULT_URL}, or LABAGENT_LLM_URL)")
    q.add_argument("--model", help=f"Model (default {nl.DEFAULT_MODEL}, or LABAGENT_LLM_MODEL)")
    q.add_argument("-y", "--yes", action="store_true", help="Run without asking for confirmation")
    q.add_argument("--print-only", action="store_true", help="Show the translated command and stop")
    a = q.parse_args(argv[1:] if argv and argv[0] == "ask" else argv)
    cfg = nl.settings(a.endpoint, a.model)
    if not nl.is_local(cfg["url"]):
        print(f"note: your request is sent to {cfg['url']}", file=sys.stderr)
    try:
        plan = nl.translate(" ".join(a.text), registry.all_packs(), registry.resolve, cfg["url"], cfg["model"], cfg["api_key"])
    except nl.NLError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for w in plan.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"Interpreted as:\n  {plan.command_line()}", file=sys.stderr)
    if a.print_only:
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            print("error: not an interactive terminal: re-run with --yes to execute, or use the command above", file=sys.stderr)
            return 2
        if plan.action == "destroy":
            ans = input(f"This deletes resources. Type the instance name ({plan.name}) to confirm: ").strip()
            if ans != plan.name:
                print("cancelled", file=sys.stderr)
                return 1
        elif input("Run it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled", file=sys.stderr)
            return 1
    return main(plan.argv())


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and (argv[0] not in KNOWN or argv[0] == "ask"):
        return _nl_main(argv)
    p = argparse.ArgumentParser(prog="labagent", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="JSON config file (or LABAGENT_CONFIG)")
    p.add_argument("--profile", help="AWS profile for local use. CI uses OIDC environment credentials.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Execute a job document: the CI entry point")
    r.add_argument("--job", required=True, help="Path to job JSON (contains roster: keep it out of git and logs)")
    r.add_argument("--output", default="result.json", help="Result envelope path, or - for stdout")
    r.add_argument("--ruleset", help="Path to a ruleset JSON for audit/cleanup (merged into the job)")
    pk = sub.add_parser("packs", help="List available lab packs")
    pk.add_argument("--json", action="store_true", help="Machine-readable catalog with parameters, platforms and cost tier (used by the console)")
    from .ai.rag import cli as rag_cli
    rag_cli.add_parser(sub)
    from .packs.gpu_notebooks import offers as offers_cli
    offers_cli.add_parser(sub)
    dp = sub.add_parser("dataset", help="Package a chunk file or a folder of tables as a versioned, checksummed snapshot (maintainers)")
    dp.add_argument("--chunks", help="JSONL of chunks: one object per line with id, title, content and meta")
    dp.add_argument("--tables", help="Folder of CSV/Parquet files with a provenance.json (for example from python -m labagent.packs.aml.gen)")
    dp.add_argument("--name", required=True)
    dp.add_argument("--version", required=True)
    dp.add_argument("--out", required=True, help="Output directory")
    for verb, text in (("launch", "Deploy a lab pack (idempotent: re-run to resume)"), ("destroy", "Remove what a pack created")):
        lp = sub.add_parser(verb, help=text)
        lp.add_argument("pack", nargs="?", help="Pack id (or give it in the launch file)")
        lp.add_argument("-f", "--file", metavar="FILE", help="YAML launch file with pack, name and params. Flags on the command line override it")
        lp.add_argument("--name", help="Instance name (state key and resource suffix); defaults to the pack's own default")
        lp.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="Pack parameter, repeatable")
        lp.add_argument("--dry-run", action="store_true", help="Show the steps without running them")
        lp.add_argument("--output", default=None, help="Result envelope path, or - for stdout (default: stdout, unless it is a terminal)")
        lp.add_argument("--json", action="store_true", help="Print the result envelope even on a terminal (the default when stdout is a pipe or a file)")
        lp.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="Colour and live progress: auto = only on a terminal, and never with NO_COLOR set")
    sub.add_parser("bootstrap-path", help="Print the path of the packaged Terraform state-backend module")
    sub.add_parser("commands", help="List supported job commands")
    args = p.parse_args(argv)

    if args.cmd == "bootstrap-path":
        print(STATE_BACKEND_DIR)
        return 0
    if args.cmd == "dataset":
        from .datasets import DatasetError, build_snapshot, build_table_snapshot
        if bool(args.chunks) == bool(args.tables):
            print("error: give exactly one of --chunks or --tables", file=sys.stderr)
            return 2
        try:
            if args.tables:
                m = build_table_snapshot(Path(args.tables).expanduser(), args.name, args.version, Path(args.out).expanduser())
            else:
                m = build_snapshot(Path(args.chunks).expanduser(), args.name, args.version, Path(args.out).expanduser())
        except DatasetError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps({k: m[k] for k in ("file", "chunks", "bytes", "sha256") if k in m}, indent=2))
        return 0
    if args.cmd == "rag":
        return rag_cli.run(args)
    if args.cmd in ("offers", "ssh-keys"):
        return offers_cli.run(args)
    if args.cmd in ("packs", "launch", "destroy"):
        return _packs_main(args)
    if args.cmd == "commands":
        print("\n".join(sorted(COMMANDS)))
        return 0
    try:
        job = json.loads(Path(args.job).expanduser().read_text(encoding="utf-8"))
        if job.get("command") in PACK_COMMANDS:       # lab packs bring their own credentials: no AWS account guard here
            env = run_pack_job(job)
            _emit(env, args.output)
            return env.exit_code
        cfg = load_config(args.config)
        session = boto3.Session(profile_name=args.profile, region_name=cfg.region)
        if getattr(args, "ruleset", None) and "ruleset" not in job:
            job["ruleset"] = json.loads(Path(args.ruleset).expanduser().read_text(encoding="utf-8"))
        env = run_job(Ctx(cfg, session), job)
    except GuardError as e:
        env = Envelope(command="guard").fail(str(e))
    except Exception as e:
        env = Envelope(command="startup").fail(f"{type(e).__name__}: {str(e)[:200]}")
    _emit(env, args.output)
    return env.exit_code


if __name__ == "__main__":
    sys.exit(main())
