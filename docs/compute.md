# labagent compute

Commands that answer "what can I rent, from whom, for how much, and what do I type into `launch`?". They create and change nothing, except `ssh-key-add`, which adds one public SSH key to your cloud account (free, and you can delete it there). The cloud is always named: there is no default.

```bash
labagent compute clouds                                          # which clouds are wired in, and whether each one's key is set
labagent compute offers --cloud shadeform --gpu H100             # available now, cheapest first, with the OS images each offer accepts
labagent compute offers --cloud shadeform --gpu H100 --gpus 8 --all
labagent compute offers --cloud shadeform --vendor scaleway --max-price 5 --kind vm --limit 10
labagent compute ssh-keys --cloud shadeform                      # the ssh_key_id values in that account, with each key's fingerprint
labagent compute ssh-key-add --cloud all --name labagent         # make ~/.labagent/keys/labagent_ed25519 if missing, add its public half to EVERY cloud
labagent compute offers --cloud lambda --gpu H100                # the same commands work on every cloud: see Lambda below
```

Every table starts with whose answer it is: `Shadeform . GPU offers . on-demand, available now, 2 offers, cheapest first . fetched 18:20Z`.

| Command | Answers |
|---|---|
| `compute clouds` | The clouds this install can talk to, what each can do (`offers`, `ssh-keys`, `launch`), and whether its API key is set (never the key) |
| `compute offers --cloud C` | Shapes you can rent from cloud `C` |
| `compute ssh-keys --cloud C` | SSH keys already in your account at `C`: id, name, whether it is the account default, and its SHA256 fingerprint. Never the key |
| `compute ssh-key-add --cloud C --name N [--file PATH]` | Makes an ed25519 key at `PATH` (default `~/.labagent/keys/labagent_ed25519`, the same file for every cloud, no passphrase, mode 0600) if the file does not exist, never overwrites one, and adds only its public half to the account. A key already there is reused. `--cloud all` does it for every cloud whose API key is set (others are skipped, and one failing does not stop the rest; exit 2 if any failed). Launches find the key by fingerprint, so the `ssh_key_id` it prints is optional |

`--cloud` is required. `labagent compute clouds` lists the values. A key comes from the cloud's environment variable (Shadeform: `SHADEFORM_API_KEY`, Lambda: `LAMBDA_API_KEY`), then the cloud's section of `~/.labagent/credentials`, then, for Lambda, the file `~/.labagent/keys/lambda_api_key` holding just the key. It is only ever sent to that cloud. A key file other users can read gets a warning: `chmod 600` it.

## Lambda

`--cloud lambda` is Lambda Cloud (formerly Lambda Labs, API at `cloud.lambda.com`). It differs from Shadeform in ways you will see:

- **One company, so no vendor.** The `vendor` of every offer is `lambda`, and you pick a `region` such as `us-west-3`. `--location europe | north-america | asia-pacific` works too (from the region code).
- **Shapes.** `--type` takes Lambda's own name (`gpu_1x_h100_pcie`) or the shape other clouds use: `H100` is the cheapest one-GPU H100, `H100x8` the eight-GPU one. Only x86 GPU shapes are listed: the ARM GH200 is left out because the lab image is for x86. Sold-out shapes (`--all`) show no region, because Lambda lists capacity per shape.
- **Images.** The default is `lambda-stack-24-04` (Ubuntu with the NVIDIA driver and CUDA). Others: `gpu-base-24-04`, `lambda-stack-22-04`, `gpu-base-22-04`. The login user is `ubuntu`.
- **SSH keys are chosen by name at launch.** The `ssh_key_id` you pass is looked up in the account, and its name is sent. `ssh-key-add` sends only the public half, so Lambda never generates a pair (and never has a private key to hand back).
- **No automatic deletion.** Lambda has no setting that deletes an instance after some hours, so `hours` cannot be enforced by the cloud. A Lambda VM bills until you `destroy` it. A launch that fails still deletes its own VM.
- **Firewall.** A new Lambda account lets in only SSH and ping, and the labs are served on 80 and 443. A launch attaches a firewall ruleset named `labagent-web-<region>` that opens exactly TCP 80 and 443 from anywhere. It is created on first use in that region and reused after. labagent never edits the account-wide rules and never deletes the ruleset (other instances may use it). Remove it in the Lambda console when you are done with it.

For `--cloud all` the JSON is `compute.ssh-key-all/1`: `name`, `fingerprint`, `key_file`, `key_file_created`, and `clouds`, one row per cloud with `status` (`ok`, `skipped` or `failed`), `ssh_key_id`, `added_to_cloud` or `error`. `status` is `failed` (with `error` and exit 2) if the key could not be added somewhere.

## offers

Filters: `--gpu` (family, e.g. H100), `--gpus` (count per machine), `--type` (the cloud's shape name, the `instance_type` parameter), `--vendor` (the datacenter company behind the offer), `--region`, `--location` (`any`, `europe`, `north-america`, `asia-pacific`: the area of the region, from its country code), `--kind` (vm, baremetal, container), `--max-price` (dollars per hour), `--all` (include sold out), `--limit`. Capacity changes within minutes, so look right before you launch. Only on-demand capacity is listed, never spot.

Each row gives `vendor`, `region` and `instance_type`, and the `os` values the offer accepts. Use `--kind vm` or `baremetal` for `gpu-notebooks`: a `container` offer has no root, Docker or MIG.

## JSON for a console or script

Add `--json`. Every document has `schema` (what it is, and its version), `status` (`ok` or `failed`) and, when it failed, `error`. Keys are only ever added, never renamed or removed. With `--json` a failure is a JSON document on stdout with exit code 2, so a caller never parses text. Warnings (for example a credentials file other users can read) are in `warnings` and on stderr.

```json
{"schema": "compute.offers/1", "status": "ok", "cloud": "shadeform", "cloud_title": "Shadeform", "fetched_at": "2026-11-13T18:20:00Z",
 "count": 1, "returned": 1, "warnings": [], "offers": [
  {"instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1", "region_name": "US, Dallas, TX", "gpu_type": "H100",
   "num_gpus": 1, "vram_gb": 80, "vcpus": 16, "memory_gb": 192, "storage_gb": 7600, "interconnect": "pcie", "kind": "baremetal", "location": "north-america",
   "hourly_usd": 1.99, "available": true, "boot_seconds": 1020, "max_labs": 7, "os_options": ["ubuntu24.04_cuda12.4_shade_os"],
   "launch": {"cloud": "shadeform", "instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1"}}]}
```

```json
{"schema": "compute.ssh-keys/1", "status": "ok", "cloud": "shadeform", "cloud_title": "Shadeform", "fetched_at": "...", "count": 2, "warnings": [],
 "ssh_keys": [{"id": "...", "name": "laptop", "is_default": false, "fingerprint": "SHA256:..."}]}
```

```json
{"schema": "compute.ssh-key/1", "status": "ok", "cloud": "shadeform", "cloud_title": "Shadeform", "fetched_at": "...", "warnings": [],
 "ssh_key_id": "...", "name": "labagent", "fingerprint": "SHA256:...", "key_file": "/home/me/.labagent/keys/labagent_ed25519",
 "key_file_created": true, "added_to_cloud": true}
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
