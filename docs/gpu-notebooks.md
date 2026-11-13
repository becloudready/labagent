# gpu-notebooks: JupyterLab labs on a rented GPU

Goal: one JupyterLab per slice of an NVIDIA H100 (MIG), for hackathons and workshops on GPU clouds. **This is part 1: the provider layer and the host.** `launch` rents one GPU VM. Splitting the GPUs and starting the notebooks comes next.

```bash
# one-time: put your key in ~/.labagent/credentials (chmod 600), or export SHADEFORM_API_KEY
#   [shadeform]
#   <your api key>
labagent launch gpu-notebooks --name test --dry-run
labagent launch gpu-notebooks --name test --param cloud=shadeform --param vendor=scaleway --param region=paris-france-1 \
    --param os=ubuntu24.04_cuda13.0_shade_os --param ssh_key_id=<id from your Shadeform account>
labagent destroy gpu-notebooks --name test
```

Leave `vendor` and `region` out and the launch stops with the five cheapest available offers for your `instance_type`.

| Param | Meaning |
|---|---|
| `cloud` | Whose API to call: `shadeform` (the default and the only one so far) |
| `instance_type` | The provider's shape: `H100` is one GPU, `H100x8` is eight |
| `vendor`, `region`, `os`, `ssh_key_id` | Shadeform options. `vendor` is the datacenter company behind the offer (`scaleway`, `massedcompute`). `os` must be one the offer lists |
| `hours` | The provider deletes the VM after this long (1 to 72, default 4), even if this tool never runs again |

**It costs money.** `launch` starts a billed VM and prints the hourly price first. Billing stops when `destroy` runs (or `hours` ends). `destroy` deletes only the VM this launch created: the id in the state file, or, after a crash, a VM with exactly this name and tag. It never matches by prefix.

**Adding a cloud.** Subclass `Provider` in `labagent/packs/gpu_notebooks/providers.py` (validate, find, create, get, delete) and add it to `PROVIDERS`: its key becomes a value of `--param cloud`.

**Verified on a real VM (2026-11-13).** One H100 PCIe on Scaleway through Shadeform (`ubuntu24.04_cuda13.0_shade_os`, driver 580, Docker and the NVIDIA container toolkit preinstalled, passwordless sudo): `launch` and `destroy` worked. Inside the VM, `nvidia-smi -mig 1` worked with no reset, `1g.10gb` x 7 gave seven devices of 9.75 GiB and 14 SMs, and seven PyTorch containers, one per `NVIDIA_VISIBLE_DEVICES=MIG-<uuid>`, ran a matmul at the same time. Each saw one GPU and all took about the same time (1.96 s), so the slices do not slow each other. Tearing MIG down (`mig -dci`, `mig -dgi`, `-mig 0`) also worked.

**Not verified yet.** MIG after a reboot (NVIDIA says Hopper does not keep it), the 8-GPU SXM shape, other clouds, and Jupyter itself. Shadeform capacity changes within minutes: a `launch` that passed the preflight earlier can be refused later, and nothing is created when it is.

**MIG limit.** An H100 splits into at most 7 slices (`1g.10gb`), so an 8-GPU box gives 56 labs, not 64.
