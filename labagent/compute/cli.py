"""`labagent compute clouds | offers | ssh-keys`: what can be rented right now, from whom, and what to type into `launch`. Read only.

    labagent compute clouds                                       which clouds are wired in, and whether their key is set
    labagent compute offers --cloud shadeform --gpu H100          a table, cheapest first
    labagent compute offers --cloud shadeform --gpu H100 --json   the same as JSON, for a console or script
    labagent compute ssh-keys --cloud shadeform                   the ssh_key_id values you can use

The JSON is a contract for a console: every document has `schema` (what it is and its version), `status` (ok or failed) and, on
failure, `error`. Keys are only ever added, never renamed or removed. With --json a failure is a JSON document on stdout and exit 2,
so a caller never has to parse text. Each offer carries `launch`, the gpu-notebooks parameters that select exactly that offer.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import providers
from .errors import ComputeError
from .providers import Offer, OfferQuery
from ..console import Console, Style

KINDS = ("vm", "baremetal", "container")
SCHEMAS = {"clouds": "compute.clouds/1", "offers": "compute.offers/1", "ssh-keys": "compute.ssh-keys/1"}

# ---- gpu-notebooks hints ----------------------------------------------------------------------------------------------
# Smallest-slice MIG instances per GPU, for the families the gpu-notebooks pack has been designed around. Anything not listed has no
# estimate (None) rather than a guess: `nvidia-smi mig -lgip` on the VM is the authority. This and `launch_params` are the only
# places this module knows about a pack.
MIG_SLICES_PER_GPU = {"H100": 7, "H100_nvl": 7, "H200": 7, "A100": 7, "A100_80G": 7}


def max_labs(o: Offer) -> Optional[int]:
    per = MIG_SLICES_PER_GPU.get(o.gpu_type)
    return per * o.num_gpus if per and o.num_gpus else None


def launch_params(cloud: str, o: Offer) -> Dict[str, str]:
    """The gpu-notebooks parameters that select this offer. `os` and `ssh_key_id` are yours to choose: see os_options and `compute ssh-keys`."""
    return {"cloud": cloud, "instance_type": o.instance_type, "vendor": o.vendor, "region": o.region}


# ---- documents --------------------------------------------------------------------------------------------------------

def offer_json(cloud: str, o: Offer) -> Dict[str, Any]:
    return {"instance_type": o.instance_type, "vendor": o.vendor, "region": o.region, "region_name": o.region_name, "gpu_type": o.gpu_type,
            "num_gpus": o.num_gpus, "vram_gb": o.vram_gb, "vcpus": o.vcpus, "memory_gb": o.memory_gb, "storage_gb": o.storage_gb,
            "interconnect": o.interconnect, "kind": o.kind, "hourly_usd": o.hourly_usd, "available": o.available,
            "boot_seconds": o.boot_seconds, "max_labs": max_labs(o), "os_options": o.os_options, "launch": launch_params(cloud, o)}


def _ok(kind: str, **body: Any) -> Dict[str, Any]:
    return {"schema": SCHEMAS[kind], "status": "ok", **body}


def _failed(kind: str, error: str) -> Dict[str, Any]:
    return {"schema": SCHEMAS[kind], "status": "failed", "error": error}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- text -------------------------------------------------------------------------------------------------------------

def _clean(value: Any) -> str:
    """A cell for a terminal. The text comes from a web API, so control characters are never passed through."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))


def _table(rows: List[List[str]], header: List[str], paint=None) -> str:
    """Columns are padded first and coloured after, so colour never shifts the alignment. paint(column name, text, is_header)."""
    grid = [header] + rows
    widths = [max(len(r[i]) for r in grid) for i in range(len(header) - 1)]
    out = []
    for n, r in enumerate(grid):
        cells = [c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]
        out.append("  ".join(paint(header[i], c, n == 0) for i, c in enumerate(cells)) if paint else "  ".join(cells))
    return "\n".join(out)


def _painter(s: Style):
    """Colours for the tables: headers bold, price green, stock green or red, the rest quiet."""
    def paint(col: str, text: str, is_header: bool) -> str:
        if is_header:
            return s(text, "bold", "underline")
        word = text.strip()
        if col in ("VENDOR", "CLOUD"):
            return s(text, "bold")
        if col == "$/H":
            return s(text, "green", "bold")
        if col == "STOCK":
            return s(text, "green" if word == "yes" else "red")
        if col == "LABS":
            return s(text, "cyan" if word != "-" else "gray")
        if col == "KIND" and word == "baremetal":
            return s(text, "magenta")
        if col in ("OS IMAGES", "REGION", "NAME", "CAN"):
            return s(text, "gray")
        if col == "DEFAULT" and word == "yes":
            return s(text, "green", "bold")
        if col == "KEY":
            return s(text, "green" if word.startswith("set") else "yellow")
        return text
    return paint


def heading(title: str, what: str, detail: str, fetched_at: Optional[str], s: Style, dot: str = "-") -> str:
    """The first line of every table: whose answer this is, and when it was asked. `Shadeform . GPU offers . 2 of 4 . fetched 18:20Z`."""
    parts = [s(_clean(title), "bold", "cyan"), what, detail]
    if fetched_at:
        parts.append("fetched " + fetched_at[11:16] + "Z")
    return (" " + s(dot, "gray") + " ").join(p if i < 2 else s(p, "gray") for i, p in enumerate(parts) if p)


def offers_text(cloud: str, offers: List[Offer], total: int, show_all: bool, s: Optional[Style] = None, title: str = "",
                fetched_at: Optional[str] = None, dot: str = "-") -> str:
    s = s or Style(False)
    shown = "on-demand" + (", including sold out" if show_all else ", available now")
    count = f"{len(offers)} of {total} offers" if total > len(offers) else f"{total} offers"
    top = heading(title or cloud, "GPU offers", f"{shown}, {count}" + (", cheapest first" if offers else ""), fetched_at, s, dot) + "\n"
    if not offers:
        return top + ("No offers match" + ("" if show_all else " right now") + ". Capacity changes within minutes: try again, widen the filters, "
                      "or add --all to include what is sold out.")
    header = ["VENDOR", "REGION", "TYPE", "GPUS", "GPU", "VRAM", "$/H", "KIND", "LABS"] + (["STOCK"] if show_all else []) + ["OS IMAGES"]
    rows = []
    for o in offers:
        labs = max_labs(o)
        rows.append([_clean(o.vendor), _clean(o.region), _clean(o.instance_type), str(o.num_gpus), _clean(o.gpu_type), f"{o.vram_gb}G",
                     f"{o.hourly_usd:.2f}", _clean(o.kind), str(labs) if labs else "-"]
                    + (["yes" if o.available else "no"] if show_all else []) + [_clean(", ".join(o.os_options)) or "-"])
    lines = [top, _table(rows, header, _painter(s) if s.on else None)]
    if total > len(offers):
        lines.append(f"\n{total - len(offers)} more not shown: narrow with --gpu, --gpus, --vendor, --region or --max-price, or raise --limit.")
    first = next((o for o in offers if o.available), None)
    if first:
        p = launch_params(cloud, first)
        flags = " ".join(f"--param {shlex.quote(f'{k}={_clean(v)}')}" for k, v in p.items())      # it is pasted into a shell
        keys = f"labagent compute ssh-keys --cloud {shlex.quote(_clean(cloud))}"
        lines.append(f"\nNext, for the first available row (pick an os above; ssh_key_id from `{keys}`):\n  "
                     + s(f"labagent launch gpu-notebooks --name <name> {flags} --param os=<image> --param ssh_key_id=<id>", "cyan"))
    lines.append(s("LABS = MIG slices at the smallest size, an estimate; - means unknown. Capacity changes within minutes.", "gray"))
    return "\n".join(lines)


def keys_text(cloud: str, keys: List[providers.SshKey], s: Optional[Style] = None, title: str = "", fetched_at: Optional[str] = None, dot: str = "-") -> str:
    s = s or Style(False)
    top = heading(title or cloud, "SSH keys", f"{len(keys)} in this account", fetched_at, s, dot) + "\n"
    if not keys:
        return top + "No SSH keys in this account. Add your public key in the cloud's console, then run this again."
    rows = [[_clean(k.id), "yes" if k.is_default else "", _clean(k.name)] for k in keys]
    return (top + _table(rows, ["SSH_KEY_ID", "DEFAULT", "NAME"], _painter(s) if s.on else None)
            + "\n\n" + s("Use one as ssh_key_id. The pack logs in from this machine, so choose a key whose private half you hold (ssh_key_file).", "gray"))


def clouds_text(clouds: List[Dict[str, Any]], s: Optional[Style] = None, dot: str = "-") -> str:
    s = s or Style(False)
    top = s("Compute clouds", "bold", "cyan") + " " + s(dot, "gray") + " " + s(f"{len(clouds)} wired in", "gray") + "\n"
    rows = []
    for c in clouds:
        cred = c["credentials"]
        key = f"set ({cred['source']})" if cred["configured"] else f"missing: set {cred['env_var']} or [{cred['section']}] in {cred['file']}"
        rows.append([_clean(c["id"]), _clean(c["title"]), _clean(", ".join(x.replace("_", "-") for x in c["capabilities"])) or "-", _clean(key)])
    return top + _table(rows, ["CLOUD", "NAME", "CAN", "KEY"], _painter(s) if s.on else None)


# ---- command line -----------------------------------------------------------------------------------------------------

def add_parser(sub) -> None:
    c = sub.add_parser("compute", help="Rent compute from a cloud: what is on offer, what it costs, your SSH keys (read only)")
    cs = c.add_subparsers(dest="compute_cmd", required=True, metavar="{clouds,offers,ssh-keys}")
    colour = dict(choices=("auto", "always", "never"), default="auto", help="Colour the table: auto = only on a terminal, never with NO_COLOR set")
    cloud = dict(metavar="CLOUD", help="Whose API to ask. Required: see `labagent compute clouds`")

    k = cs.add_parser("clouds", help="Which clouds this install can talk to, and whether each one's API key is set")
    k.add_argument("--json", action="store_true", help="Machine-readable output for a console or script")
    k.add_argument("--color", **colour)

    o = cs.add_parser("offers", help="List GPU machines you can rent right now from one cloud, cheapest first")
    o.add_argument("--cloud", **cloud)
    o.add_argument("--gpu", metavar="TYPE", help="GPU family, e.g. H100, H200, A100, L40S")
    o.add_argument("--gpus", type=int, metavar="N", help="Exactly this many GPUs per machine, e.g. 8")
    o.add_argument("--type", dest="instance_type", metavar="SHAPE", help="The cloud's shape name, e.g. H100 or H100x8_NVLINK (the instance_type parameter)")
    o.add_argument("--vendor", help="The datacenter company behind the offer, e.g. scaleway")
    o.add_argument("--region", help="e.g. paris-france-1")
    o.add_argument("--kind", choices=KINDS, help="vm or baremetal can run this pack's labs; container cannot")
    o.add_argument("--max-price", type=float, metavar="USD", help="At most this many dollars per hour")
    o.add_argument("--all", action="store_true", help="Include shapes that are sold out right now")
    o.add_argument("--limit", type=int, metavar="N", help="Show only the N cheapest")
    o.add_argument("--json", action="store_true", help="Machine-readable output for a console or script")
    o.add_argument("--color", **colour)

    s = cs.add_parser("ssh-keys", help="List the SSH keys in one cloud account: the ssh_key_id values for launch")
    s.add_argument("--cloud", **cloud)
    s.add_argument("--json", action="store_true", help="Machine-readable output for a console or script")
    s.add_argument("--color", **colour)


def _provider(args) -> providers.Provider:
    if not getattr(args, "cloud", None):
        raise ComputeError(f"--cloud is required. Available: {', '.join(sorted(providers.PROVIDERS))}. See them with: labagent compute clouds")
    return providers.make(args.cloud)


def _query(args) -> OfferQuery:
    if args.limit is not None and args.limit < 1:
        raise ComputeError("--limit must be 1 or more")
    if args.gpus is not None and args.gpus < 1:
        raise ComputeError("--gpus must be 1 or more")
    return OfferQuery(gpu_type=args.gpu, num_gpus=args.gpus, instance_type=args.instance_type, vendor=args.vendor, region=args.region,
                      kind=args.kind, max_price=args.max_price, available_only=not args.all)


def run(args) -> int:
    kind = args.compute_cmd
    try:
        out = _run(args, kind, Console(sys.stdout, args.color))
    except ComputeError as e:
        if args.json:
            print(json.dumps(_failed(kind, str(e)), indent=2))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2) if args.json else out)
    return 0


def _run(args, kind: str, con: Console):
    """The JSON document when --json, else the text. Raises ComputeError."""
    dot, s = con.sym["dot"], con.s
    if kind == "clouds":
        clouds = providers.catalog()
        for c in clouds:
            for w in c["warnings"]:
                print(f"warning: {w}", file=sys.stderr)
        return _ok("clouds", clouds=clouds) if args.json else clouds_text(clouds, s, dot)
    query = _query(args) if kind == "offers" else None            # a bad flag is refused before any call to the cloud
    provider = _provider(args)
    for w in getattr(provider, "warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    title, fetched, warnings = getattr(provider, "title", "") or args.cloud, _now(), list(getattr(provider, "warnings", []))
    if kind == "ssh-keys":
        keys = provider.ssh_keys()
        if args.json:
            return _ok("ssh-keys", cloud=args.cloud, cloud_title=title, fetched_at=fetched, count=len(keys), warnings=warnings,
                       ssh_keys=[{"id": k.id, "name": k.name, "is_default": k.is_default} for k in keys])
        return keys_text(args.cloud, keys, s, title, fetched, dot)
    found = provider.offers(query)
    shown = found[:args.limit] if args.limit else found
    if args.json:
        return _ok("offers", cloud=args.cloud, cloud_title=title, fetched_at=fetched, count=len(found), returned=len(shown), warnings=warnings,
                   offers=[offer_json(args.cloud, o) for o in shown])
    return offers_text(args.cloud, shown, len(found), args.all, s, title, fetched, dot)
