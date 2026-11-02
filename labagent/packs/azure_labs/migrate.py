"""Lift and shift: read a source resource group (read-only), then copy its VMs, disks and network into a target group.

The source is never changed. Disks are copied through snapshots, so the new VMs boot the same operating system, accounts and data.
Every resource created in the target keeps its source name and carries this migration's tags, so destroy can find exactly what was made.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Set, Tuple

from ..base import PackError

SNAPSHOT = "microsoft.compute/snapshots"
DISK = "microsoft.compute/disks"


def last(rid: str) -> str:
    return rid.rstrip("/").split("/")[-1]


def after(rid: str, segment: str) -> str:
    parts = rid.rstrip("/").split("/")
    return parts[[p.lower() for p in parts].index(segment.lower()) + 1]


def _rule(r: Dict[str, Any]) -> Dict[str, Any]:
    p = r["properties"]

    def many(single: str, plural: str) -> List[str]:
        return [p[single]] if p.get(single) else list(p.get(plural) or ["*"])
    return {"name": r["name"], "priority": p["priority"], "direction": p["direction"], "access": p["access"], "protocol": p["protocol"],
            "source_addresses": many("sourceAddressPrefix", "sourceAddressPrefixes"), "source_ports": many("sourcePortRange", "sourcePortRanges"),
            "destination_addresses": many("destinationAddressPrefix", "destinationAddressPrefixes"),
            "destination_ports": many("destinationPortRange", "destinationPortRanges")}


def inspect(az: Any, source: str, wanted: List[str], snapshot_only: bool) -> Dict[str, Any]:
    """What would be copied. Raises one PackError listing everything that cannot be copied, so nothing is half done."""
    vms = az.list_vms(source)
    if wanted:
        missing = sorted(set(wanted) - {v["name"] for v in vms})
        if missing:
            raise PackError(f"VM(s) not found in {source}: {', '.join(missing)}")
        vms = [v for v in vms if v["name"] in wanted]
    disks = {d["id"].lower(): d for d in az.list_disks(source)}
    problems: List[str] = []
    inv: Dict[str, Any] = {"vms": [], "disks": [], "nics": {}, "nsgs": {}, "vnets": {}, "pips": {}}
    nsg_ids: Dict[str, str] = {}
    used: Set[str] = set()
    for v in vms:
        name = v["name"]
        if v.get("availabilitySet") or v.get("virtualMachineScaleSet"):
            problems.append(f"{name}: is in an availability set or scale set")
        osd = v["storageProfile"]["osDisk"]
        vm_disks = [osd] + sorted(v["storageProfile"].get("dataDisks") or [], key=lambda d: d.get("lun", 0))
        entries = []
        for d in vm_disks:
            md = d.get("managedDisk")
            if not md:
                problems.append(f"{name}: disk {d.get('name')} is an unmanaged VHD (convert it to a managed disk first)")
                continue
            info = disks.get(md["id"].lower())
            if info is None:
                problems.append(f"{name}: disk {d.get('name')} is not in {source}")
                continue
            if (info.get("encryption") or {}).get("diskEncryptionSetId") or info.get("encryptionSettingsCollection"):
                problems.append(f"{name}: disk {info['name']} uses customer-managed or Azure Disk Encryption keys")
            used.add(info["id"].lower())
            entries.append({"name": info["name"], "id": info["id"], "sku": (info.get("sku") or {}).get("name", "Premium_LRS"),
                            "hyper_v": info.get("hyperVGeneration"), "os_type": info.get("osType"), "location": info["location"]})
        if not entries:
            continue
        nic_names = []
        for ref in v["networkProfile"]["networkInterfaces"]:
            nic = az.show(ref["id"])
            cfgs = nic["properties"]["ipConfigurations"]
            if len(cfgs) != 1:
                problems.append(f"{name}: NIC {nic['name']} has more than one IP configuration")
                continue
            ip = cfgs[0]["properties"]
            sid = ip["subnet"]["id"]
            nsg_id = (nic["properties"].get("networkSecurityGroup") or {}).get("id")
            pip_id = (ip.get("publicIPAddress") or {}).get("id")
            inv["nics"][nic["name"]] = {"vnet": after(sid, "virtualNetworks"), "subnet": last(sid), "vnet_id": sid.split("/subnets/")[0],
                                        "nsg": last(nsg_id) if nsg_id else None, "pip": last(pip_id) if pip_id else None,
                                        "accelerated": bool(nic["properties"].get("enableAcceleratedNetworking"))}
            nic_names.append(nic["name"])
            if nsg_id:
                nsg_ids[last(nsg_id)] = nsg_id
            if pip_id:
                inv["pips"][last(pip_id)] = {"location": nic["location"]}
        inv["vms"].append({"name": name, "size": v["hardwareProfile"]["vmSize"], "location": v["location"],
                           "zone": (v.get("zones") or [None])[0], "os_type": (entries[0]["os_type"] or "Linux").lower(),
                           "disks": entries, "nics": nic_names})
    if snapshot_only and not wanted:                                  # also the disks nothing is using: the "old dead" ones
        for did, d in disks.items():
            if did not in used and not d.get("managedBy"):
                inv["disks"].append({"name": d["name"], "id": d["id"], "location": d["location"], "hyper_v": d.get("hyperVGeneration")})
    for nic in inv["nics"].values():
        if nic["vnet"] not in inv["vnets"]:
            vn = az.show(nic["vnet_id"])
            subs = []
            for s in vn["properties"]["subnets"]:
                sp = s["properties"]
                prefix = sp.get("addressPrefix") or (sp.get("addressPrefixes") or [None])[0]
                nsg = (sp.get("networkSecurityGroup") or {}).get("id")
                if nsg:
                    nsg_ids[last(nsg)] = nsg
                subs.append({"name": s["name"], "prefix": prefix, "nsg": last(nsg) if nsg else None})
            inv["vnets"][nic["vnet"]] = {"prefixes": vn["properties"]["addressSpace"]["addressPrefixes"], "location": vn["location"], "subnets": subs}
    for nsg_name, nsg_id in nsg_ids.items():
        rules = az.show(nsg_id)["properties"].get("securityRules") or []
        if any(r["properties"].get("sourceApplicationSecurityGroups") or r["properties"].get("destinationApplicationSecurityGroups") for r in rules):
            problems.append(f"NSG {nsg_name}: rules use application security groups")
        inv["nsgs"][nsg_name] = [_rule(r) for r in rules]
    if not inv["vms"] and not inv["disks"]:
        problems.append(f"nothing to copy in {source}")
    if problems:
        raise PackError("cannot migrate: " + "; ".join(problems))
    return inv


def existing(az: Any, target: str) -> Set[Tuple[str, str]]:
    return {(r["type"].lower(), r["name"].lower()) for r in az.resources(target)} if az.group_exists(target) else set()


def has(seen: Set[Tuple[str, str]], rtype: str, name: str) -> bool:
    return (rtype.lower(), name.lower()) in seen


def snapshot_name(disk: str) -> str:
    return f"snap-{disk}"[:80]


def snapshots(az: Any, inv: Dict[str, Any], target: str, tags: Dict[str, str], log: Callable[[str], None]) -> int:
    seen = existing(az, target)
    made = 0
    for d in [x for v in inv["vms"] for x in v["disks"]] + inv["disks"]:
        sn = snapshot_name(d["name"])
        if has(seen, SNAPSHOT, sn):
            continue
        az.snapshot_create(target, sn, d["id"], d["location"], tags, d.get("hyper_v"))
        log(f"  snapshot {sn}")
        made += 1
    return made


def network(az: Any, inv: Dict[str, Any], target: str, tags: Dict[str, str], log: Callable[[str], None]) -> int:
    seen = existing(az, target)
    made = 0
    for nsg, rules in inv["nsgs"].items():
        if has(seen, "microsoft.network/networksecuritygroups", nsg):
            continue
        loc = next(iter(inv["vnets"].values()))["location"]
        az.nsg_create(target, nsg, loc, tags)
        for r in rules:
            az.nsg_rule_create(target, nsg, r)
        log(f"  network security group {nsg} ({len(rules)} rules)")
        made += 1
    for vname, vn in inv["vnets"].items():
        if has(seen, "microsoft.network/virtualnetworks", vname):
            continue
        first, rest = vn["subnets"][0], vn["subnets"][1:]
        az.vnet_create(target, vname, vn["prefixes"], first["name"], first["prefix"], vn["location"], tags)
        for s in rest:
            az.subnet_create(target, vname, s["name"], s["prefix"])
        for s in vn["subnets"]:
            if s["nsg"]:
                az.subnet_set_nsg(target, vname, s["name"], s["nsg"])
        log(f"  virtual network {vname} ({len(vn['subnets'])} subnets)")
        made += 1
    for pip, info in inv["pips"].items():
        if not has(seen, "microsoft.network/publicipaddresses", pip):
            az.public_ip_create(target, pip, info["location"], tags)
            log(f"  public IP {pip} (a new address: the old one is not moved)")
            made += 1
    for nname, n in inv["nics"].items():
        if has(seen, "microsoft.network/networkinterfaces", nname):
            continue
        loc = next(v["location"] for v in inv["vms"] if nname in v["nics"])
        az.nic_create(target, nname, n["vnet"], n["subnet"], loc, tags, nsg=n["nsg"], public_ip=n["pip"], accelerated=n["accelerated"])
        log(f"  network interface {nname}")
        made += 1
    return made


def disks(az: Any, inv: Dict[str, Any], target: str, tags: Dict[str, str], log: Callable[[str], None]) -> int:
    seen = existing(az, target)
    snaps = {r["name"]: r["id"] for r in az.resources(target) if r["type"].lower() == SNAPSHOT}
    made = 0
    for v in inv["vms"]:
        for i, d in enumerate(v["disks"]):
            if has(seen, DISK, d["name"]):
                continue
            snap = snaps.get(snapshot_name(d["name"]))
            if snap is None:
                raise PackError(f"snapshot of {d['name']} is not in {target} yet: run launch again")
            az.disk_create(target, d["name"], snap, d["sku"], d["location"], tags, zone=v["zone"], hyper_v=d.get("hyper_v"),
                           os_type=d["os_type"] if i == 0 else None)
            log(f"  disk {d['name']}")
            made += 1
    return made


def vms(az: Any, inv: Dict[str, Any], target: str, tags: Dict[str, str], log: Callable[[str], None]) -> int:
    seen = existing(az, target)
    ids = {(r["type"].lower(), r["name"]): r["id"] for r in az.resources(target)}
    made = 0
    for v in inv["vms"]:
        if has(seen, "microsoft.compute/virtualmachines", v["name"]):
            continue
        os_disk, data = v["disks"][0], v["disks"][1:]
        az.vm_create(target, v["name"], ids[(DISK, os_disk["name"])], v["os_type"], v["size"], [ids[("microsoft.network/networkinterfaces", n)] for n in v["nics"]],
                     [ids[(DISK, d["name"])] for d in data], v["location"], tags, zone=v["zone"])
        log(f"  VM {v['name']} ({v['size']}, {1 + len(data)} disk(s))")
        made += 1
    return made
