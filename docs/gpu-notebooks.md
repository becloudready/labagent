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

Leave `vendor` and `region` out and the launch stops with the five cheapest available offers for your `instance_type`.

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
| `profile` | Slice size: `1g.10gb` (7 per H100), `1g.20gb` (4), `2g.20gb` (3), `3g.40gb` (2) |
| `labs` | How many labs to start (0 = one per slice, at most 99) |
| `image` | Docker image per lab (default `quay.io/jupyter/pytorch-notebook:cuda12-latest`) |
| `tls` | `letsencrypt` (default), `internal` (self-signed) or `none` (plain http: tokens travel in clear) |
| `domain` | Name the labs are served on (default `<ip>.sslip.io`) |
| `hostname` | Host name of the VM, shown in every shell prompt. Default: the launch `--name`, so name launches after the event |
| `login_user` | Shared shell user (default `labagent`, empty = none) |
| `login_sudo` | Give that user sudo and docker (default true) |

## The shared login

The user gets a random 20 character password (in `<name>-ssh-login.csv`, 0600, and in the final report), ssh password login for that one user only (every other user stays key-only), your ssh key too, and a prompt that shows `user@host`. A user someone created by hand is repaired: shell, home directory, prompt.

**With sudo this is root.** Anyone with the password can read every lab's token, stop other labs and change the MIG slices, which defeats the one-container-per-slice isolation. For students, give out the lab URLs and set `login_sudo=false`, or keep the shell login for instructors.

## Cost and safety

`launch` starts a billed VM and prints the hourly price first. `destroy` deletes only the VM this launch created: the id in the state file, or after a crash a VM with exactly this name and tag, never by prefix. Tokens go to the VM on ssh stdin, never on a command line, and are not in state or the result JSON.

## Verified on a real VM (2026-11-13)

One H100 PCIe on Scaleway through Shadeform (`ubuntu24.04_cuda13.0_shade_os`, driver 580, Docker and the NVIDIA container toolkit preinstalled): launch, 7 slices, 7 JupyterLab containers behind a Let's Encrypt certificate, token enforcement, password login for the shared user, the prompt, and destroy. Seven PyTorch containers on seven slices ran at once with no slowdown from each other.

**Not verified yet:** a reboot of the VM (NVIDIA says Hopper does not keep MIG, so the boot unit rebuilds it; that unit has not been rebooted), the 8-GPU SXM shape, other clouds, and many learners using the labs at once. Shadeform capacity changes within minutes: an offer that passed the preflight can be gone a moment later, and nothing is created when it is.

**MIG limit.** An H100 splits into at most 7 slices, so an 8-GPU box gives 56 labs, not 64.

**Adding a cloud.** Subclass `Provider` in `labagent/packs/gpu_notebooks/providers.py` (validate, find, create, get, delete) and add it to `PROVIDERS`: its key becomes a value of `--param cloud`.
