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
    sub.add_parser("bootstrap-path", help="Print the path of the packaged Terraform state-backend module")
    sub.add_parser("commands", help="List supported job commands")
    args = p.parse_args(argv)

    if args.cmd == "bootstrap-path":
        print(STATE_BACKEND_DIR)
        return 0
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
