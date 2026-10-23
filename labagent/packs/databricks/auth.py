"""Shared login handling for Databricks packs."""
from __future__ import annotations

from typing import Any, Dict

from ..base import PackContext, PackError


def login(ctx: PackContext) -> Dict[str, Any]:
    """Return `current-user me`. If the CLI's default profile is expired while exactly one other profile for the SAME
    workspace is valid, use that one and say so. Never crosses workspaces, never overrides an explicit profile."""
    d = ctx.services["dbx"]
    try:
        return d.json("current-user", "me")
    except PackError as first:
        if ctx.params.get("profile") or not hasattr(d, "profiles"):
            raise
        profiles = d.profiles()
        default = next((p for p in profiles if p.get("default")), None)
        valid = [p for p in profiles if p.get("valid")]
        same = [p for p in valid if default and p.get("host") == default.get("host")]
        if len(same) == 1:
            ctx.log(f"  default profile '{default['name']}' is expired: using '{same[0]['name']}' (same workspace)")
            d.profile = same[0]["name"]
            return d.json("current-user", "me")
        names = ", ".join(p["name"] for p in valid) or "none"
        raise PackError(f"{first} Profiles with a valid login: {names}. Choose one with --param profile=<name> "
                        f"or DATABRICKS_CONFIG_PROFILE, or sign in again: databricks auth login") from first


def workspace_host(ctx: PackContext) -> str:
    """https://<workspace host> for the profile in use."""
    d = ctx.services["dbx"]
    prof = d.profile or next((p["name"] for p in d.profiles() if p.get("default")), None)
    for p in d.profiles():
        if p.get("name") == prof and p.get("host"):
            return str(p["host"]).rstrip("/")
    raise PackError("could not determine the workspace host from the Databricks CLI profile")
