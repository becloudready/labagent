"""Lab packs: self-contained, idempotent launch/destroy recipes for one lab each.

A pack is a Python class (see base.Pack). Built-in packs live in this package; third parties register more
through the `labagent.packs` entry-point group, so private or client-specific labs never need a fork.
"""
