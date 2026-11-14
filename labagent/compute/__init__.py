"""Rent compute from a cloud: what is on offer, what it costs, which SSH keys the account has, and the host lifecycle a pack needs.

Three layers, each usable without the one above it:

    labagent.compute.providers   the library. A `Provider` per cloud; no printing, no argparse, no pack knowledge.
                                 `make("shadeform").offers(OfferQuery(gpu_type="H100"))`, `catalog()`.
    labagent compute ...         the command line: a thin printer over the library. `--json` is the wire format for a console or script.
    labagent.packs.*             packs that rent hosts (gpu-notebooks) import the library, never a specific cloud.

To add a cloud, subclass `Provider` in `providers.py`, give it an `id`, `title`, `capabilities` and `env_var`, and register it in `PROVIDERS`.
`labagent compute clouds` then lists it, and every command takes it as `--cloud <id>`.
"""
from .errors import ComputeError
from .providers import Host, HostSpec, Offer, OfferQuery, Provider, SshKey, catalog, make

__all__ = ["ComputeError", "Host", "HostSpec", "Offer", "OfferQuery", "Provider", "SshKey", "catalog", "make"]
