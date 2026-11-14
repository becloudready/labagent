"""`labagent offers` and `labagent ssh-keys`: what can be rented right now, and what to type into `launch`. Read only.

    labagent offers --gpu H100                     a table, cheapest first
    labagent offers --gpu H100 --gpus 8 --json     the same as JSON, for a console or script
    labagent ssh-keys                              the ssh_key_id values you can use

The JSON shape is a contract for the console: keys are only ever added, never renamed or removed. Each offer carries `launch`,
the pack parameters that select exactly that offer.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..base import PackError
from . import providers
from ...console import Console, Style
from .providers import Offer, OfferQuery

# Smallest-slice MIG instances per GPU, for the families this pack has been designed around. Anything not listed has no
# estimate (None) rather than a guess: `nvidia-smi mig -lgip` on the VM is the authority.
MIG_SLICES_PER_GPU = {"H100": 7, "H100_nvl": 7, "H200": 7, "A100": 7, "A100_80G": 7}
KINDS = ("vm", "baremetal", "container")


def max_labs(o: Offer) -> Optional[int]:
    per = MIG_SLICES_PER_GPU.get(o.gpu_type)
    return per * o.num_gpus if per and o.num_gpus else None


def launch_params(cloud: str, o: Offer) -> Dict[str, str]:
    """The pack parameters that select this offer. `os` and `ssh_key_id` are yours to choose: see os_options and `ssh-keys`."""
    return {"cloud": cloud, "instance_type": o.instance_type, "vendor": o.vendor, "region": o.region}


def offer_json(cloud: str, o: Offer) -> Dict[str, Any]:
    return {"instance_type": o.instance_type, "vendor": o.vendor, "region": o.region, "region_name": o.region_name, "gpu_type": o.gpu_type,
            "num_gpus": o.num_gpus, "vram_gb": o.vram_gb, "vcpus": o.vcpus, "memory_gb": o.memory_gb, "storage_gb": o.storage_gb,
            "interconnect": o.interconnect, "kind": o.kind, "hourly_usd": o.hourly_usd, "available": o.available,
            "boot_seconds": o.boot_seconds, "max_labs": max_labs(o), "os_options": o.os_options, "launch": launch_params(cloud, o)}


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
        if col == "VENDOR":
            return s(text, "bold")
        if col == "$/H":
            return s(text, "green", "bold")
        if col == "STOCK":
            return s(text, "green" if word == "yes" else "red")
        if col == "LABS":
            return s(text, "cyan" if word != "-" else "gray")
        if col == "KIND" and word == "baremetal":
            return s(text, "magenta")
        if col in ("OS IMAGES", "REGION"):
            return s(text, "gray")
        if col == "DEFAULT" and word == "yes":
            return s(text, "green", "bold")
        return text
    return paint


def offers_text(cloud: str, offers: List[Offer], total: int, show_all: bool, s: Optional[Style] = None) -> str:
    s = s or Style(False)
    if not offers:
        return ("No offers match" + ("" if show_all else " right now") + ". Capacity changes within minutes: try again, widen the filters, "
                "or add --all to include what is sold out.")
    header = ["VENDOR", "REGION", "TYPE", "GPUS", "GPU", "VRAM", "$/H", "KIND", "LABS"] + (["STOCK"] if show_all else []) + ["OS IMAGES"]
    rows = []
    for o in offers:
        labs = max_labs(o)
        rows.append([_clean(o.vendor), _clean(o.region), _clean(o.instance_type), str(o.num_gpus), _clean(o.gpu_type), f"{o.vram_gb}G",
                     f"{o.hourly_usd:.2f}", _clean(o.kind), str(labs) if labs else "-"]
                    + (["yes" if o.available else "no"] if show_all else []) + [_clean(", ".join(o.os_options)) or "-"])
    lines = [_table(rows, header, _painter(s) if s.on else None)]
    if total > len(offers):
        lines.append(f"\n{total - len(offers)} more not shown: narrow with --gpu, --gpus, --vendor, --region or --max-price, or raise --limit.")
    first = next((o for o in offers if o.available), None)
    if first:
        p = launch_params(cloud, first)
        flags = " ".join(f"--param {shlex.quote(f'{k}={_clean(v)}')}" for k, v in p.items())      # it is pasted into a shell
        lines.append(f"\nNext, for the first available row (pick an os above; ssh_key_id from `labagent ssh-keys`):\n  "
                     + s(f"labagent launch gpu-notebooks --name <name> {flags} --param os=<image> --param ssh_key_id=<id>", "cyan"))
    lines.append(s("LABS = MIG slices at the smallest size, an estimate; - means unknown. Capacity changes within minutes.", "gray"))
    return "\n".join(lines)


def keys_text(keys: List[providers.SshKey], s: Optional[Style] = None) -> str:
    s = s or Style(False)
    if not keys:
        return "No SSH keys in this account. Add your public key in the cloud's console, then run this again."
    rows = [[_clean(k.id), "yes" if k.is_default else "", _clean(k.name)] for k in keys]
    return (_table(rows, ["SSH_KEY_ID", "DEFAULT", "NAME"], _painter(s) if s.on else None)
            + "\n\n" + s("Use one as ssh_key_id. The pack logs in from this machine, so choose a key whose private half you hold (ssh_key_file).", "gray"))


def add_parser(sub) -> None:
    o = sub.add_parser("offers", help="List GPU machines you can rent right now, cheapest first (read only)")
    o.add_argument("--cloud", default="shadeform", help="Whose API to ask (default shadeform)")
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
    o.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Colour the table: auto = only on a terminal, never with NO_COLOR set")
    k = sub.add_parser("ssh-keys", help="List the SSH keys in your cloud account: the ssh_key_id values for launch (read only)")
    k.add_argument("--cloud", default="shadeform", help="Whose API to ask (default shadeform)")
    k.add_argument("--json", action="store_true", help="Machine-readable output")
    k.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Colour the table: auto = only on a terminal, never with NO_COLOR set")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(args) -> int:
    try:
        provider = providers.make(args.cloud)
        for w in getattr(provider, "warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        if args.cmd == "ssh-keys":
            keys = provider.ssh_keys()
            if args.json:
                print(json.dumps({"cloud": args.cloud, "fetched_at": _now(), "count": len(keys),
                                  "ssh_keys": [{"id": k.id, "name": k.name, "is_default": k.is_default} for k in keys]}, indent=2))
            else:
                print(keys_text(keys, Console(sys.stdout, args.color).s))
            return 0
        if args.limit is not None and args.limit < 1:
            raise PackError("--limit must be 1 or more")
        if args.gpus is not None and args.gpus < 1:
            raise PackError("--gpus must be 1 or more")
        found = provider.offers(OfferQuery(gpu_type=args.gpu, num_gpus=args.gpus, instance_type=args.instance_type, vendor=args.vendor,
                                           region=args.region, kind=args.kind, max_price=args.max_price, available_only=not args.all))
    except PackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    shown = found[:args.limit] if args.limit else found
    if args.json:
        print(json.dumps({"cloud": args.cloud, "fetched_at": _now(), "count": len(found), "returned": len(shown),
                          "offers": [offer_json(args.cloud, o) for o in shown]}, indent=2))
    else:
        print(offers_text(args.cloud, shown, len(found), args.all, Console(sys.stdout, args.color).s))
    return 0
