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
from .run import COMMANDS, PACK_COMMANDS, run_job, run_pack_job


def _emit(env: Envelope, output: str) -> None:
    text = json.dumps(env.to_dict(), indent=2)
    if output == "-":
        print(text)
    else:
        Path(output).write_text(text, encoding="utf-8")
        Path(output).chmod(0o600)


def _read_pubkey(path):
    """PEM text of the orchestrator's public key from --pubkey, or the file named by LABAGENT_PUBKEY. None when neither is set."""
    import os
    path = path or os.environ.get("LABAGENT_PUBKEY")
    return Path(path).expanduser().read_text(encoding="utf-8") if path else None


def _secrets_main(args) -> int:
    from . import secrets_box
    if args.secrets_cmd == "keygen":
        out = Path(args.dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        private, public = secrets_box.generate_keypair()
        priv_path, pub_path = out / "labagent-private.pem", out / "labagent-public.pem"
        for path, text, mode in ((priv_path, private, 0o600), (pub_path, public, 0o644)):
            if path.exists():
                print(f"error: {path} already exists: refusing to overwrite a key", file=sys.stderr)
                return 2
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)
        print(f"private key: {priv_path} (keep it secret)\npublic key:  {pub_path} (pass to launch --pubkey)")
        return 0
    try:
        key = secrets_box.load_private_key(Path(args.key).expanduser().read_text(encoding="utf-8"))
        raw = sys.stdin.read() if args.infile == "-" else Path(args.infile).expanduser().read_text(encoding="utf-8")
        doc = json.loads(raw)
        rows = [(s["user"], s.get("kind", ""), secrets_box.decrypt(key, s["ciphertext"])) for s in doc.get("secrets", [])]
    except (OSError, ValueError, KeyError) as e:
        print(f"error: could not decrypt: {type(e).__name__}", file=sys.stderr)
        return 2
    for user, kind, pw in rows:
        print(f"{user}\t{kind}\t{pw}")
    return 0


def _sweep_azure_main(args) -> int:
    from .packs.azure_labs.azcli import AzCli
    from .packs.base import PackError
    from . import azure_sweep
    try:
        az = AzCli(args.subscription)
        sub = az.account()["id"]
        if args.sweep_cmd == "plan":
            env = azure_sweep.sweep_plan(az, sub)
        else:
            doc = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))
            env = azure_sweep.sweep_apply(az, sub, doc.get("plan") or doc, args.hash, log=lambda m: print(m, file=sys.stderr))
    except (PackError, OSError, ValueError) as e:
        env = Envelope(command="sweep-azure").fail(str(e)[:400])
    _emit(env, args.output)
    return env.exit_code


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
    try:
        pack = registry.resolve(args.pack)
        given = dict(kv.split("=", 1) for kv in args.param)
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    name = args.name or pack.default_name
    if not name:
        print("error: --name is required for this pack", file=sys.stderr)
        return 2
    try:
        pubkey = _read_pubkey(getattr(args, "pubkey", None))
    except OSError as e:
        print(f"error: cannot read the public key: {e}", file=sys.stderr)
        return 2
    env = run_pack(pack, args.cmd, name, given, dry_run=args.dry_run, pubkey_pem=pubkey)
    _emit(env, args.output)
    return env.exit_code


KNOWN = {"run", "packs", "rag", "dataset", "launch", "destroy", "bootstrap-path", "commands", "sweep-azure", "secrets"}


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
    dp = sub.add_parser("dataset", help="Package a chunk file or a folder of tables as a versioned, checksummed snapshot (maintainers)")
    dp.add_argument("--chunks", help="JSONL of chunks: one object per line with id, title, content and meta")
    dp.add_argument("--tables", help="Folder of CSV/Parquet files with a provenance.json (for example from python -m labagent.packs.aml.gen)")
    dp.add_argument("--name", required=True)
    dp.add_argument("--version", required=True)
    dp.add_argument("--out", required=True, help="Output directory")
    for verb, text in (("launch", "Deploy a lab pack (idempotent: re-run to resume)"), ("destroy", "Remove what a pack created")):
        lp = sub.add_parser(verb, help=text)
        lp.add_argument("pack")
        lp.add_argument("--name", help="Instance name (state key and resource suffix); defaults to the pack's own default")
        lp.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="Pack parameter, repeatable")
        lp.add_argument("--dry-run", action="store_true", help="Show the steps without running them")
        lp.add_argument("--output", default="-", help="Result envelope path, or - for stdout")
        if verb == "launch":
            lp.add_argument("--pubkey", help="PEM public key: passwords a pack creates are returned encrypted to it (or set LABAGENT_PUBKEY to the file path)")
    sw = sub.add_parser("sweep-azure", help="Delete azure-labs labs past their expiry: plan first, then apply that plan")
    sw.add_argument("--subscription", help="Subscription ID or name (default: the one az login selected)")
    sws = sw.add_subparsers(dest="sweep_cmd", required=True)
    swp = sws.add_parser("plan", help="List expired labs and write a plan (changes nothing)")
    swp.add_argument("--output", default="-", help="Plan envelope path, or - for stdout")
    swa = sws.add_parser("apply", help="Delete exactly the labs in a reviewed plan")
    swa.add_argument("--plan", required=True, help="Plan file from `sweep-azure plan`")
    swa.add_argument("--hash", required=True, help="The plan hash you approved")
    swa.add_argument("--output", default="-", help="Result envelope path, or - for stdout")
    sc = sub.add_parser("secrets", help="Create a key pair, or decrypt the passwords in a result envelope")
    scs = sc.add_subparsers(dest="secrets_cmd", required=True)
    kg = scs.add_parser("keygen", help="Write labagent-private.pem and labagent-public.pem")
    kg.add_argument("--dir", default=".", help="Folder for the key files")
    dc = scs.add_parser("decrypt", help="Print user, kind and password for every secret in a result envelope")
    dc.add_argument("--key", required=True, help="Private key PEM")
    dc.add_argument("--in", dest="infile", default="-", help="Result envelope JSON (default: stdin)")
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
    if args.cmd in ("packs", "launch", "destroy"):
        return _packs_main(args)
    if args.cmd == "sweep-azure":
        return _sweep_azure_main(args)
    if args.cmd == "secrets":
        return _secrets_main(args)
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
