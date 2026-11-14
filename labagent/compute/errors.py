"""One error type for the compute layer, so a caller can catch exactly what a cloud can do wrong."""
from __future__ import annotations

from ..packs.base import PackError


class ComputeError(PackError):
    """A cloud call that cannot be done and why, in words a person can act on. Never contains a key.

    It is a PackError so a pack that calls a provider shows the message as it always did, and the compute commands report it the same way.
    """
