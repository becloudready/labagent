# gpu-notebooks: one JupyterLab per GPU slice

Rent a GPU VM, split each GPU into NVIDIA MIG slices, and give every slice its own Docker container with JupyterLab. One launch prints a URL and a login for every lab. Built for hackathons and workshops on GPU clouds: an H100 gives 7 labs, an 8-GPU box 56.

```bash
# one-time: put your key in ~/.labagent/credentials (chmod 600), or export SHADEFORM_API_KEY
#   [shadeform]
#   <your api key>
labagent launch gpu-notebooks --name nvidia-h100-hackathon --dry-run
labagent launch gpu-notebooks --name nvidia-h100-hackathon \
    --param cloud=shadeform --param vendor=scaleway --param region=paris-france-1 \
    --param os=ubuntu24.04_cuda13.0_shade_os --param ssh_key_id=<id from your Shadeform account>
labagent destroy gpu-notebooks --name nvidia-h100-hackathon
```

The same run from a file ([launch files](launch-files.md)): `labagent launch -f hackathon.yaml`, and `labagent destroy -f hackathon.yaml` to stop it.

Leave `vendor` and `region` out and the launch stops with the five cheapest available offers for your `instance_type`.

## See what is available first

Two read-only commands answer "what can I launch right now, and what do I type?". They create and change nothing.

```bash
labagent offers --gpu H100                      # available now, cheapest first, with the OS images each offer accepts
labagent offers --gpu H100 --gpus 8 --all       # eight-GPU boxes, including the sold-out ones (STOCK column)
labagent offers --vendor scaleway --max-price 5 --kind vm --limit 10
labagent ssh-keys                               # the ssh_key_id values in your account
```

Filters: `--gpu` (family, e.g. H100), `--gpus` (count per machine), `--type` (the cloud's shape name, the `instance_type` param), `--vendor`, `--region`, `--kind` (vm, baremetal, container), `--max-price` (dollars per hour), `--limit`. Capacity changes within minutes, so look right before you launch. Only on-demand capacity is listed, never spot.

Each row gives `vendor`, `region` and `instance_type` for `launch`, and the `os` values the offer accepts. `LABS` is the number of smallest MIG slices (7 per GPU on H100, H200 and A100), so an 8-GPU box shows 56. It is an estimate, and `-` means unknown or no MIG: an RTX 5090 shows `-` because there you choose `labs` yourself (`mode=shared`). Your `profile` must exist on that GPU: `nvidia-smi mig -lgip` on the VM lists them. Use `--kind vm` or `baremetal`: a `container` offer has no root, Docker or MIG.

`ssh_key_id` must be a key whose private half is on the machine running `labagent` (or give `ssh_key_file`). `Shadeform Managed Key` is created by Shadeform: the pack can log in with it only if you hold that private key.

For a console or script, add `--json`. The shape is stable: keys are only ever added.

```json
{"cloud": "shadeform", "fetched_at": "2026-11-13T18:20:00Z", "count": 1, "returned": 1, "offers": [
  {"instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1", "region_name": "US, Dallas, TX", "gpu_type": "H100",
   "num_gpus": 1, "vram_gb": 80, "vcpus": 16, "memory_gb": 192, "storage_gb": 7600, "interconnect": "pcie", "kind": "baremetal",
   "hourly_usd": 1.99, "available": true, "boot_seconds": 1020, "max_labs": 7, "os_options": ["ubuntu24.04_cuda12.4_shade_os"],
   "launch": {"cloud": "shadeform", "instance_type": "H100", "vendor": "latitude", "region": "dallas-usa-1"}}]}
```

`launch` holds the pack parameters that select that offer. Add `os` from `os_options` and your `ssh_key_id`. `ssh-keys --json` gives `{"cloud", "fetched_at", "count", "ssh_keys": [{"id", "name", "is_default"}]}`. Key material is never returned. Errors go to stderr with exit code 2.

## What you see

In a terminal the run is laid out for people: a banner, numbered steps with a live timer and a tick with the time each one took, warnings in yellow, and at the end one card with the VM, its expiry, the stop command, every lab link, and the shared login. Tables from `labagent offers` and `labagent ssh-keys` are coloured the same way.

- On a terminal the result JSON is not printed, because the card says the same. `--json` prints it anyway, and `--output FILE` writes it.
- Piped, redirected, or in CI, nothing changes: plain text on stderr and the JSON on stdout.
- `NO_COLOR=1` or `--color never` gives the plain output even on a terminal. `--color always` (or `FORCE_COLOR=1`) forces colour, for example to record a demo through a pipe.

## What it does

1. **preflight**: checks the offer and prints the hourly price. Creates nothing.
2. **host**: creates the VM and waits until it is active. The cloud deletes it after `hours` whatever happens to this tool.
3. **setup**: logs in over ssh, installs the lab installer (`/opt/labagent/start-labs.sh`), enables MIG, creates the slices.
4. **access**: sets the host name and creates the shared login user (below).
5. **labs**: one container per slice (`NVIDIA_VISIBLE_DEVICES=MIG-<uuid>`), each with its own random token, CPU and memory limits, and JupyterLab under `/lab-NN`. Caddy serves them on `https://<ip>.sslip.io` with a real certificate. A systemd unit rebuilds the slices and containers at boot.
6. **verify**: every lab must answer, refuse requests without its token, accept its own token, and see exactly one GPU slice. Then the pack checks from your machine that the public URLs open.
7. **report**: prints every URL with its login, and writes `~/.labagent/packs/gpu-notebooks/<name>-credentials.csv` (0600).

## Params

| Param | Meaning |
|---|---|
| `cloud` | Whose API to call: `shadeform` (default, the only one so far) |
| `instance_type` | The cloud's shape: `H100` is one GPU, `H100x8` is eight |
| `vendor`, `region`, `os`, `ssh_key_id` | Shadeform options. `vendor` is the datacenter company behind the offer (`scaleway`, `massedcompute`). `os` must be one the offer lists |
| `ssh_key_file` | Private key that matches `ssh_key_id` (default: whatever ssh finds) |
| `hours` | The cloud deletes the VM after this long (1 to 72, default 4) |
| `mode` | `mig` (default): one hardware-isolated MIG slice per lab, on H100, H200 and A100. `shared`: labs share the whole GPU with no isolation, for GPUs without MIG such as the RTX 5090. See [GPUs](#gpus) |
| `mig_profile` | `mig` only. Slice size. `auto` (default) is the smallest the GPU offers: `1g.10gb` on H100 and A100 80GB, `1g.5gb` on A100 40GB. Others: `1g.20gb` (4 per 80GB GPU), `2g.20gb` (3), `3g.40gb` (2) |
| `labs` | How many labs to start. `mig`: 0 = one per slice. `shared`: required. At most 99 |
| `image` | Docker image per lab (default `quay.io/jupyter/pytorch-notebook:cuda12-latest`) |
| `tls` | `letsencrypt` (default), `internal` (self-signed) or `none` (plain http: tokens travel in clear) |
| `domain` | Name the labs are served on (default `<ip>.sslip.io`) |
| `hostname` | Host name of the VM, shown in every shell prompt. Default: the launch `--name`, so name launches after the event |
| `login_user` | Shared shell user (default `labagent`, empty = none) |
| `login_sudo` | Give that user sudo and docker (default true) |

## GPUs

| GPU | `mode` | Labs | Notes |
|---|---|---|---|
| H100 80GB | `mig` | 7 per GPU, 9.75 GiB each | Verified on one PCIe card. |
| A100 80GB | `mig` | 7 per GPU, same slices as H100 | Not verified on real hardware yet. |
| A100 40GB | `mig` | 7 per GPU, 4.75 GiB each (`1g.5gb`) | Not verified on real hardware yet. `mig_profile: auto` picks the size: the H100 default `1g.10gb` does not exist on a 40GB card. |
| RTX 5090, RTX 4090 | `shared` | Your choice, `labs=N` | No MIG, so no isolation. Not verified on real hardware yet. |

Find what is available and what it costs with `labagent offers --gpu A100` (families: `A100` is the 40GB card, `A100_80G` the 80GB one).

**A100: a reboot may happen.** On an A100, turning MIG on can need a GPU reset, which a VM often cannot do. The pack tries the reset, and if MIG is still not active it reboots the VM once, waits for it (up to 10 minutes), and carries on. You will see `rebooting the VM once` in the output. A second request for a reboot is a failure: destroy the VM and try another offer.

**RTX 5090 and other GPUs without MIG.** A 5090 has 32 GB and no MIG. `mode=mig` is refused before anything is rented. `mode=shared` starts `labs` containers that each see the whole GPU, taken in turn when there are several GPUs. There is **no isolation**: no memory limit and no fair share, so one learner who loads a large model can make everyone else's notebook fail with an out-of-memory error. Keep `labs` small (4 labs is about 8 GB each if everyone behaves) and tell learners to size their work, for example `torch.cuda.set_per_process_memory_fraction(0.25)`. Use `mig` when learners must not affect each other.

```bash
labagent launch gpu-notebooks --name rtx-workshop --param instance_type=RTX5090 --param vendor=<vendor> --param region=<region> \
    --param os=<os from labagent offers> --param ssh_key_id=<id> --param mode=shared --param labs=4
```

## The shared login

The user gets a random 20 character password (in `<name>-ssh-login.csv`, 0600, and in the final report), ssh password login for that one user only (every other user stays key-only), your ssh key too, and a prompt that shows `user@host`. A user someone created by hand is repaired: shell, home directory, prompt.

**With sudo this is root.** Anyone with the password can read every lab's token, stop other labs and change the MIG slices, which defeats the one-container-per-slice isolation. For students, give out the lab URLs and set `login_sudo=false`, or keep the shell login for instructors.

## Cost and safety

`launch` starts a billed VM and prints the hourly price first. `destroy` deletes only the VM this launch created: the id in the state file, or after a crash a VM with exactly this name and tag, never by prefix. Tokens go to the VM on ssh stdin, never on a command line, and are not in state or the result JSON.

## Verified on a real VM (2026-11-13)

One H100 PCIe on Scaleway through Shadeform (`ubuntu24.04_cuda13.0_shade_os`, driver 580, Docker and the NVIDIA container toolkit preinstalled): launch, 7 slices, 7 JupyterLab containers behind a Let's Encrypt certificate, token enforcement, password login for the shared user, the prompt, and destroy. Seven PyTorch containers on seven slices ran at once with no slowdown from each other.

**Not verified yet:** A100 (both sizes), the RTX 5090 shared mode and the A100 reboot path (they are tested against a stand-in for the GPU driver, not real cards), a reboot of the VM (NVIDIA says Hopper does not keep MIG, so the boot unit rebuilds it; that unit has not been rebooted), the 8-GPU SXM shape, other clouds, and many learners using the labs at once. Shadeform capacity changes within minutes: an offer that passed the preflight can be gone a moment later, and nothing is created when it is.

**MIG limit.** An H100 splits into at most 7 slices, so an 8-GPU box gives 56 labs, not 64.

**Adding a cloud.** Subclass `Provider` in `labagent/packs/gpu_notebooks/providers.py` (offers, ssh_keys, validate, find, create, get, delete) and add it to `PROVIDERS`: its key becomes a value of `--param cloud`.
