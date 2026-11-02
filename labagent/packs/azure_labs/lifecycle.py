"""Quota arithmetic and tag-only teardown, shared by the pack and by `labagent sweep-azure`."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from ..base import PackError
from . import safety

# Delete in this order so dependants go first (a NIC cannot outlive its VM, a subnet needs its NSG until the VNet is gone).
ORDER = ["microsoft.compute/virtualmachines", "microsoft.compute/virtualmachinescalesets", "microsoft.devtestlab/schedules",
         "microsoft.network/networkinterfaces", "microsoft.compute/disks", "microsoft.network/publicipaddresses",
         "microsoft.network/natgateways", "microsoft.network/virtualnetworks", "microsoft.network/networksecuritygroups"]
ROUNDS = 4


def peak_demand(size_family: str, vcpus: int, students: int, gateway_family: str, gateway_vcpus: int) -> Tuple[int, Dict[str, int]]:
    """vCPUs needed at the busiest moment: (regional total, per family).

    The template build (one VM) and the class (students plus gateway) never run together, so take the larger of the two."""
    template = {size_family: vcpus}
    klass: Dict[str, int] = {size_family: vcpus * students}
    klass[gateway_family] = klass.get(gateway_family, 0) + gateway_vcpus
    fams = {f: max(template.get(f, 0), klass.get(f, 0)) for f in set(template) | set(klass)}
    return max(sum(template.values()), sum(klass.values())), fams


def check_quota(usage: Dict[str, Dict[str, int]], region_total: int, families: Dict[str, int], region: str) -> None:
    def free(key: str) -> int:
        u = usage.get(key)
        return (u["limit"] - u["used"]) if u else 0
    short = []
    if free("cores") < region_total:
        short.append(f"total regional vCPUs: need {region_total}, free {free('cores')}")
    for fam, need in sorted(families.items()):
        if free(fam) < need:
            short.append(f"{fam}: need {need}, free {free(fam)}")
    if short:
        raise PackError(f"not enough vCPU quota in {region} ({'; '.join(short)}). Use fewer students or a smaller size, or ask for more: "
                        "Azure portal > Subscriptions > Usage + quotas.")


def _rank(rtype: str) -> int:
    t = rtype.lower()
    return ORDER.index(t) if t in ORDER else len(ORDER)


def _is_child(rtype: str) -> bool:
    """microsoft.compute/virtualmachines/extensions: deleted with its parent."""
    return rtype.count("/") > 1


def destroy_owned(az: Any, group: str, name: str, log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Delete only the resources in `group` that carry lab `name`'s tags, then the group if labagent created it and it is empty.

    Raises PackError, deleting nothing more, when something without this lab's tags is in the group."""
    info = az.group_show(group)
    if info is None:
        return {"group_deleted": False, "resources_deleted": 0, "group_found": False}
    if not safety.owned(info.get("tags"), name):
        raise PackError(f"resource group {group} does not carry this lab's tags: refusing to touch it")
    deleted = 0
    for _ in range(ROUNDS):
        mine = [r for r in az.resources(group) if safety.owned(r.get("tags"), name) and not _is_child(r["type"])]
        if not mine:
            break
        failed = 0
        for r in sorted(mine, key=lambda x: _rank(x["type"])):
            try:
                az.resource_delete(r["id"])
                deleted += 1
                log(f"  deleted {r['type'].split('/')[-1]} {r['name']}")
            except PackError:
                failed += 1                         # often a dependency not gone yet: the next round retries
        if not failed:
            break
    left = az.resources(group)
    mine_left = [r for r in left if safety.owned(r.get("tags"), name) and not _is_child(r["type"])]
    foreign = [r for r in left if not safety.owned(r.get("tags"), name) and not _is_child(r["type"])]
    if mine_left:
        raise PackError(f"could not delete {len(mine_left)} resource(s) in {group}: {', '.join(sorted(r['name'] for r in mine_left))}. Run destroy again.")
    if foreign:
        raise PackError(f"{len(foreign)} resource(s) in {group} do not carry this lab's tags and were not deleted: "
                        f"{', '.join(sorted(r['name'] for r in foreign))}. Remove them yourself, then run destroy again.")
    created = (info.get("tags") or {}).get("created_by_labagent") == "1"
    if created:
        az.group_delete(group)
    return {"group_deleted": created, "resources_deleted": deleted, "group_found": True}
