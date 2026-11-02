"""vCPU quota arithmetic and tag-only teardown."""
from __future__ import annotations

from typing import Any, Callable, Dict

from ..base import PackError
from . import safety

# Delete in this order so dependants go first.
ORDER = ["microsoft.compute/virtualmachines", "microsoft.network/networkinterfaces", "microsoft.compute/disks", "microsoft.compute/snapshots",
         "microsoft.network/publicipaddresses", "microsoft.network/virtualnetworks", "microsoft.network/networksecuritygroups"]
ROUNDS = 4


def check_quota(usage: Dict[str, Dict[str, int]], needs: Dict[str, int], region: str) -> None:
    """`needs` is vCPUs per VM family. Also checks the regional total."""
    def free(key: str) -> int:
        u = usage.get(key)
        return (u["limit"] - u["used"]) if u else 0
    total = sum(needs.values())
    short = [f"total regional vCPUs: need {total}, free {free('cores')}"] if free("cores") < total else []
    short += [f"{fam}: need {n}, free {free(fam)}" for fam, n in sorted(needs.items()) if free(fam) < n]
    if short:
        raise PackError(f"not enough vCPU quota in {region} ({'; '.join(short)}). The source VMs still count while they run: stop them, "
                        "migrate fewer VMs at once (vms=a,b), or ask for more in the Azure portal under Subscriptions > Usage + quotas.")


def _rank(rtype: str) -> int:
    t = rtype.lower()
    return ORDER.index(t) if t in ORDER else len(ORDER)


def destroy_owned(az: Any, group: str, name: str, log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Delete only the resources in `group` that carry this migration's tags, then the group if labagent created it and it is empty.

    Raises PackError, deleting nothing more, when something without these tags is left. The source group is never passed here."""
    info = az.group_show(group)
    if info is None:
        return {"group_deleted": False, "resources_deleted": 0}
    deleted = 0
    for _ in range(ROUNDS):
        mine = [r for r in az.resources(group) if safety.owned(r.get("tags"), name) and r["type"].count("/") == 1]
        if not mine:
            break
        failed = 0
        for r in sorted(mine, key=lambda x: _rank(x["type"])):
            try:
                az.resource_delete(r["id"])
                deleted += 1
                log(f"  deleted {r['type'].split('/')[-1]} {r['name']}")
            except PackError:
                failed += 1                          # usually a dependency that is not gone yet: the next round retries
        if not failed:
            break
    left = [r for r in az.resources(group) if r["type"].count("/") == 1]
    mine_left = [r["name"] for r in left if safety.owned(r.get("tags"), name)]
    foreign = [r["name"] for r in left if not safety.owned(r.get("tags"), name)]
    if mine_left:
        raise PackError(f"could not delete {', '.join(sorted(mine_left))} in {group}. Run destroy again.")
    created = (info.get("tags") or {}).get("created_by_labagent") == "1"
    if foreign:
        if created:
            raise PackError(f"{len(foreign)} resource(s) in {group} do not carry this migration's tags and were not deleted: "
                            f"{', '.join(sorted(foreign))}. Remove them yourself, then run destroy again.")
        return {"group_deleted": False, "resources_deleted": deleted}
    if created:
        az.group_delete(group)
    return {"group_deleted": created, "resources_deleted": deleted}
