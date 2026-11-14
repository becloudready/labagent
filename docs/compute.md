# labagent compute

Read-only commands that answer "what can I rent, from whom, for how much, and what do I type into `launch`?". They create and change nothing. The cloud is always named: there is no default.

```bash
labagent compute clouds                                          # which clouds are wired in, and whether each one's key is set
labagent compute offers --cloud shadeform --gpu H100             # available now, cheapest first, with the OS images each offer accepts
labagent compute offers --cloud shadeform --gpu H100 --gpus 8 --all
labagent compute offers --cloud shadeform --vendor scaleway --max-price 5 --kind vm --limit 10
labagent compute ssh-keys --cloud shadeform                      # the ssh_key_id values in that account
```

Every table starts with whose answer it is: `Shadeform . GPU offers . on-demand, available now, 2 offers, cheapest first . fetched 18:20Z`.

| Command | Answers |
|---|---|
| `compute clouds` | The clouds this install can talk to, what each can do (`offers`, `ssh-keys`, `launch`), and whether its API key is set (never the key) |
| `compute offers --cloud C` | Shapes you can rent from cloud `C` |
| `compute ssh-keys --cloud C` | SSH keys already in your account at `C` |

`--cloud` is required. `labagent compute clouds` lists the values. A key comes from the cloud's environment variable (Shadeform: `SHADEFORM_API_KEY`) or `~/.labagent/credentials`, and is only ever sent to that cloud.

## offers

Filters: `--gpu` (family, e.g. H100), `--gpus` (count per machine), `--type` (the cloud's shape name, the `instance_type` parameter), `--vendor` (the datacenter company behind the offer), `--region`, `--kind` (vm, baremetal, container), `--max-price` (dollars per hour), `--all` (include sold out), `--limit`. Capacity changes within minutes, so look right before you launch. Only on-demand capacity is listed, never spot.

Each row gives `vendor`, `region` and `instance_type`, and the `os` values the offer accepts. Use `--kind vm` or `baremetal` for `gpu-notebooks`: a `container` offer has no root, Docker or MIG.

## JSON for a console or script

Add `--json`. Every document has `schema` (what it is, and its version), `status` (`ok` or `failed`) and, when it failed, `error`. Keys are only ever added, never renamed or removed. With `--json` a failure is a JSON document on stdout with exit code 2, so a caller never parses text. Warnings (for example a credentials file other users can read) are in `warnings` and on stderr.

```json
{"schema": "compute.offers/1", "status": "ok", "cloud": "shadeform", "cloud_title": "Shadeform", "fetched_at": "2026-11-13T18:20:00Z",
 "count": 1, "returned": 1, "warnings": [], "offers": [
  {"instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1", "region_name": "US, Dallas, TX", "gpu_type": "H100",
   "num_gpus": 1, "vram_gb": 80, "vcpus": 16, "memory_gb": 192, "storage_gb": 7600, "interconnect": "pcie", "kind": "baremetal",
   "hourly_usd": 1.99, "available": true, "boot_seconds": 1020, "max_labs": 7, "os_options": ["ubuntu24.04_cuda12.4_shade_os"],
   "launch": {"cloud": "shadeform", "instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1"}}]}
```

```json
{"schema": "compute.ssh-keys/1", "status": "ok", "cloud": "shadeform", "cloud_title": "Shadeform", "fetched_at": "...", "count": 2, "warnings": [],
 "ssh_keys": [{"id": "...", "name": "laptop", "is_default": false}]}
```

```json
{"schema": "compute.clouds/1", "status": "ok", "clouds": [
  {"id": "shadeform", "title": "Shadeform", "capabilities": ["offers", "ssh_keys", "launch"], "warnings": [],
   "credentials": {"configured": true, "source": "environment", "env_var": "SHADEFORM_API_KEY", "file": "~/.labagent/credentials", "section": "shadeform"}}]}
```

```json
{"schema": "compute.offers/1", "status": "failed", "error": "--cloud is required. Available: shadeform. See them with: labagent compute clouds"}
```

`launch` holds the `gpu-notebooks` parameters that select that offer, and `max_labs` is its estimate of MIG slices (an estimate: `null` means unknown). Both are hints for that one pack. Add `os` from `os_options` and your `ssh_key_id`. Key material is never returned.

## From Python

The commands are a thin printer over a library with no printing and no pack knowledge:

```python
from labagent import compute

compute.catalog()                                              # the same list as `compute clouds`
cloud = compute.make("shadeform")                              # reads the key from the environment or ~/.labagent/credentials
offers = cloud.offers(compute.OfferQuery(gpu_type="H100", num_gpus=8))
keys = cloud.ssh_keys()                                        # compute.ComputeError says what went wrong, and never contains a key
```

## Adding a cloud

Subclass `Provider` in `labagent/compute/providers.py`, set `id`, `title`, `capabilities` and `env_var`, implement `offers`, `ssh_keys` and (for `launch`) the host methods, and add it to `PROVIDERS`. `compute clouds` lists it and every command takes it as `--cloud <id>`. Nothing else changes.
