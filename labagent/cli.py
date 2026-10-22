"""labagent command line. stdout is silent unless --output - is given; logs go to stderr (ids and counts only)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

from . import __version__
from .config import STATE_BACKEND_DIR, load_config
from .context import Ctx, GuardError
from .envelope import Envelope
from .run import COMMANDS, run_job


def _emit(env: Envelope, output: str) -> None:
    text = json.dumps(env.to_dict(), indent=2)
    if output == "-":
        print(text)
    else:
        Path(output).write_text(text)
        Path(output).chmod(0o600)


def _packs_main(args) -> int:
    from .packs import registry
    from .packs.base import run_pack
    if args.cmd == "packs":
        for pid, pk in sorted(registry.all_packs().items()):
            print(f"{pid:16} {pk.summary}")
        return 0
    try:
        pack = registry.get(args.pack)
        given = dict(kv.split("=", 1) for kv in args.param)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    env = run_pack(pack, args.cmd, args.name, given, dry_run=args.dry_run)
    _emit(env, args.output)
    return env.exit_code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="labagent", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="JSON config file (or LABAGENT_CONFIG)")
    p.add_argument("--profile", help="AWS profile for local use. CI uses OIDC environment credentials.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Execute a job document: the CI entry point")
    r.add_argument("--job", required=True, help="Path to job JSON (contains roster: keep it out of git and logs)")
    r.add_argument("--output", default="result.json", help="Result envelope path, or - for stdout")
    r.add_argument("--ruleset", help="Path to a ruleset JSON for audit/cleanup (merged into the job)")
    sub.add_parser("packs", help="List available lab packs")
    dp = sub.add_parser("dataset", help="Package a chunk file as a versioned, checksummed snapshot (maintainers)")
    dp.add_argument("--chunks", required=True, help="JSONL of chunks, e.g. ~/.labagent/cache/rag-sec-filings/<name>.jsonl")
    dp.add_argument("--name", required=True)
    dp.add_argument("--version", required=True)
    dp.add_argument("--out", required=True, help="Output directory")
    for verb, text in (("launch", "Deploy a lab pack (idempotent: re-run to resume)"), ("destroy", "Remove what a pack created")):
        lp = sub.add_parser(verb, help=text)
        lp.add_argument("pack")
        lp.add_argument("--name", required=True, help="Instance name (state key and resource suffix)")
        lp.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="Pack parameter, repeatable")
        lp.add_argument("--dry-run", action="store_true", help="Show the steps without running them")
        lp.add_argument("--output", default="-", help="Result envelope path, or - for stdout")
    sub.add_parser("bootstrap-path", help="Print the path of the packaged Terraform state-backend module")
    sub.add_parser("commands", help="List supported job commands")
    args = p.parse_args(argv)

    if args.cmd == "bootstrap-path":
        print(STATE_BACKEND_DIR)
        return 0
    if args.cmd == "dataset":
        from .datasets import DatasetError, build_snapshot
        try:
            m = build_snapshot(Path(args.chunks), args.name, args.version, Path(args.out))
        except DatasetError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps({k: m[k] for k in ("file", "chunks", "bytes", "sha256")}, indent=2))
        return 0
    if args.cmd in ("packs", "launch", "destroy"):
        return _packs_main(args)
    if args.cmd == "commands":
        print("\n".join(sorted(COMMANDS)))
        return 0
    try:
        cfg = load_config(args.config)
        job = json.loads(Path(args.job).read_text())
        session = boto3.Session(profile_name=args.profile, region_name=cfg.region)
        if getattr(args, "ruleset", None) and "ruleset" not in job:
            job["ruleset"] = json.loads(Path(args.ruleset).read_text())
        env = run_job(Ctx(cfg, session), job)
    except GuardError as e:
        env = Envelope(command="guard").fail(str(e))
    except Exception as e:
        env = Envelope(command="startup").fail(f"{type(e).__name__}: {str(e)[:200]}")
    _emit(env, args.output)
    return env.exit_code


if __name__ == "__main__":
    sys.exit(main())
