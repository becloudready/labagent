# gpu-notebooks: JupyterLab labs on a rented GPU

Goal: one JupyterLab per slice of an NVIDIA H100 (MIG), for hackathons and workshops on GPU clouds. **This is part 1: the provider layer and the host.** `launch` rents one GPU VM. Splitting the GPUs and starting the notebooks comes next.

```bash
# one-time: put your key in ~/.labagent/credentials (chmod 600), or export SHADEFORM_API_KEY
#   [shadeform]
#   <your api key>
labagent launch gpu-notebooks --name test --dry-run
labagent launch gpu-notebooks --name test --param cloud=massedcompute --param region=desmoines-usa-1 \
    --param os=ubuntu22.04_cuda13.0_shade_os --param ssh_key_id=<id from your Shadeform account>
labagent destroy gpu-notebooks --name test
```

Leave `cloud` and `region` out and the launch stops with the five cheapest available offers for your `instance_type`.

| Param | Meaning |
|---|---|
| `provider` | `shadeform` (the only one so far) |
| `instance_type` | The provider's shape: `H100` is one GPU, `H100x8` is eight |
| `cloud`, `region`, `os`, `ssh_key_id` | Shadeform options. `os` must be one the offer lists |
| `hours` | The provider deletes the VM after this long (1 to 72, default 4), even if this tool never runs again |

**It costs money.** `launch` starts a billed VM and prints the hourly price first. Billing stops when `destroy` runs (or `hours` ends). `destroy` deletes only the VM this launch created: the id in the state file, or, after a crash, a VM with exactly this name and tag. It never matches by prefix.

**Adding a cloud.** Subclass `Provider` in `labagent/packs/gpu_notebooks/providers.py` (validate, find, create, get, delete) and add it to `PROVIDERS`.

**How far to trust it.** The Shadeform calls follow its API docs and were checked against the live price list (read only). Creating and deleting a real VM has not been run yet.

**MIG limit.** An H100 splits into at most 7 slices (`1g.10gb`), so an 8-GPU box gives 56 labs, not 64.
