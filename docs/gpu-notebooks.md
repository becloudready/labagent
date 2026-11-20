# gpu-notebooks: one JupyterLab per GPU slice

Rent a GPU VM, split each GPU into NVIDIA MIG slices, and give every slice its own Docker container with JupyterLab. One launch prints a URL and a login for every lab. Built for hackathons and workshops on GPU clouds: an H100 gives 7 labs, an 8-GPU box 56.

```bash
# one-time: put your key in ~/.labagent/credentials (chmod 600), or export SHADEFORM_API_KEY
#   [shadeform]
#   <your api key>
# one-time: make a key just for launches (kept apart from ~/.ssh) and add its public half to your Shadeform account
labagent compute ssh-key-add --cloud all --name labagent             # ONE key for every cloud that has an API key set
labagent launch gpu-notebooks --name nvidia-h100-hackathon --dry-run
labagent launch gpu-notebooks --name nvidia-h100-hackathon \
    --param cloud=shadeform --param vendor=scaleway --param region=paris-france-1 \
    --param os=ubuntu24.04_cuda13.0_shade_os
labagent destroy gpu-notebooks --name nvidia-h100-hackathon
```

On Lambda Cloud instead (key in `LAMBDA_API_KEY`, `~/.labagent/credentials` under `[lambda]`, or `~/.labagent/keys/lambda_api_key`; see [compute.md](compute.md#lambda)): `--param cloud=lambda --param region=us-west-3`, or `--param location=north-america` to take the cheapest. **Lambda never deletes a VM for you:** it bills until `destroy`.

On Nebius (a project of yours in one region, `NEBIUS_PROJECT_ID`, and a token, a service account or the `nebius` CLI; see [compute.md](compute.md#nebius)): `--param cloud=nebius` (the region is the project's). **Nebius never deletes a VM for you either**, and there is no SSH key list: the key is installed on the VM at creation from your private key file.

The same run from a file ([launch files](launch-files.md)): `labagent launch -f hackathon.yaml`, and `labagent destroy -f hackathon.yaml` to stop it.

Leave `vendor` and `region` out and the launch stops with the five cheapest available offers for your `instance_type`. Or set `location` instead (`any`, `europe`, `north-america`, `asia-pacific`) and labagent picks the cheapest available machine of that `instance_type` there, with the newest CUDA image. It keeps that pick in the pack's state, so a rerun never switches machine.

## See what is available first

`labagent compute offers --cloud shadeform --gpu H100` shows what you can launch right now, cheapest first, and what to type. `labagent compute ssh-keys --cloud shadeform` shows the `ssh_key_id` values. Both are read only. Commands, filters and the JSON for a console are in [compute.md](compute.md).

```bash
labagent compute offers --cloud shadeform --gpu H100
labagent compute offers --cloud shadeform --gpu H100 --gpus 8 --all       # eight-GPU boxes, including the sold-out ones
labagent compute ssh-keys --cloud shadeform
```

`LABS` is the number of smallest MIG slices (7 per GPU on H100, H200 and A100), so an 8-GPU box shows 56. It is an estimate, and `-` means unknown or no MIG: an RTX 5090 shows `-` because there you choose `labs` yourself (`mode=shared`). Your `mig_profile` must exist on that GPU: `nvidia-smi mig -lgip` on the VM lists them.

## The SSH key

The launch logs in to the new VM with an SSH key, so the cloud account must hold the public half of a private file you have. **One key serves every cloud**: you keep one private file, and each cloud gets its public half under whatever id it likes.

- Make it once: `labagent compute ssh-key-add --cloud all --name labagent`. It makes `~/.labagent/keys/labagent_ed25519` (no passphrase, mode 0600) if it is missing, never overwrites a file, and adds only the public half to every cloud that has an API key set. Run it twice and nothing is added twice. `--cloud shadeform` (or any one cloud) adds it to just that cloud, and `--file PATH` uses another private file (for example an existing key you already registered in one cloud, to add it to the others).
- Use a key made only for launches, not your everyday `~/.ssh/id_ed25519`.
- That is all: **there is no `ssh_key_id` to copy**. The launch takes the private file (`ssh_key_file`, else `LABAGENT_SSH_KEY_FILE`, else `~/.labagent/keys/labagent_ed25519` if it exists) and finds the key in the cloud account whose fingerprint matches. If the account has no such key, nothing is rented and the message gives the command that adds it. Without any key the cloud would install its own default one (`Shadeform Managed Key`), whose private half is not on your machine, and the launch could never log in: that caused a real failed launch.
- To use a different key in one account, set `ssh_key_id` (see `labagent compute ssh-keys --cloud <cloud>`). Then the launch compares the private file with that key, and a mismatch stops it with both fingerprints and nothing rented. Ids differ per cloud, so only use this with one cloud.
- The result (`summary.outputs`) says which key was used: `ssh_key_id`, `ssh_key_name`, `ssh_key_fingerprint` and `ssh_key_verified` (false when no file was given or the cloud shows no public half, so it could not be checked). The file's path is not in the result.
- A key with a passphrase cannot be used: nothing can type it during a launch.

## What you see

In a terminal the run is laid out for people: a banner, numbered steps with a live timer and a tick with the time each one took, warnings in yellow, and at the end one card with the VM, its expiry, the stop command, every lab link, and the shared login. Tables from `labagent offers` and `labagent ssh-keys` are coloured the same way.

- On a terminal the result JSON is not printed, because the card says the same. `--json` prints it anyway, and `--output FILE` writes it.
- Piped, redirected, or in CI, nothing changes: plain text on stderr and the JSON on stdout.
- `NO_COLOR=1` or `--color never` gives the plain output even on a terminal. `--color always` (or `FORCE_COLOR=1`) forces colour, for example to record a demo through a pipe.

## What it does

1. **preflight**: checks the offer and prints the hourly price. Creates nothing.
2. **host**: creates the VM and waits until it is active. Shadeform deletes it after `hours` whatever happens to this tool. Lambda and Nebius have no such setting: there the VM bills until `destroy`, and the preflight, the report and `summary.outputs.auto_delete` (`false`) all say so.
3. **setup**: logs in over ssh, installs the lab installer (`/opt/labagent/start-labs.sh`), enables MIG, creates the slices.
4. **access**: sets the host name and creates the shared login user (below).
5. **labs**: one container per slice (`NVIDIA_VISIBLE_DEVICES=MIG-<uuid>`), each with its own random token, CPU and memory limits, and JupyterLab under `/lab-NN`. Caddy serves them on `https://<ip>.sslip.io` with a real certificate. A systemd unit rebuilds the slices and containers at boot.
6. **verify**: every lab must answer, refuse requests without its token, accept its own token, and see exactly one GPU slice. Then the pack checks from your machine that the public URLs open.
7. **report**: prints every URL with its login, and writes `~/.labagent/packs/gpu-notebooks/<name>-credentials.csv` (0600).

## Params

| Param | Meaning |
|---|---|
| `cloud` | Whose API to call: `shadeform` (default), `lambda` or `nebius` |
| `instance_type` | The cloud's shape: `H100` is one GPU, `H100x8` is eight |
| `location` | `any`, `europe`, `north-america` or `asia-pacific`. Used only when `vendor` and `region` are empty: picks the cheapest available machine of `instance_type` in that area (never a container) and the newest CUDA `os`. Fails with a plain message when nothing is available there |
| `max_price` | Dollars per hour, default 0 (no limit). Used with `location`: dearer offers are ignored, and the launch fails plainly if none is left |
| `show_passwords` | `yes` (default) or `no`. `no` keeps every lab token and the shell password out of the printed report and the card. They stay in the credentials file, and a job with a public key gets the tokens back sealed. The console always sets `no` |
| `vendor`, `region`, `os`, `ssh_key_id` | `vendor` is Shadeform only: the datacenter company behind the offer (`scaleway`, `massedcompute`). `region` is that vendor's region on Shadeform, e.g. `us-west-3` on Lambda, and on Nebius the region of the project (e.g. `eu-north1`). `os` must be one the offer lists (on Lambda an image family such as `lambda-stack-24-04`, the default). `ssh_key_id` is optional, see [The SSH key](#the-ssh-key); Nebius has no key list, so it is ignored there |
| `ssh_key_file` | The private key to log in with. Default: the `LABAGENT_SSH_KEY_FILE` environment variable, then `~/.labagent/keys/labagent_ed25519` if it exists. Its public half must be in the cloud account (found by fingerprint, or checked against `ssh_key_id` when you set that) before anything is rented. A remote job cannot set it |
| `keep_on_failure` | `no` (default) or `yes`. A launch that fails deletes the VM it rented (see [Cost and safety](#cost-and-safety)). `yes` leaves it running so you can look inside; you pay until `destroy` or `hours`. A remote job cannot set it |
| `hours` | Shadeform deletes the VM after this long (1 to 72, default 4). Lambda and Nebius cannot: there it only sets the planned end shown in the report, and you must run `destroy` |
| `mode` | `mig` (default): one hardware-isolated MIG slice per lab, on H100, H200 and A100. `shared`: labs share the whole GPU with no isolation, for GPUs without MIG such as the RTX 5090. See [GPUs](#gpus) |
| `mig_profile` | `mig` only. Slice size. `auto` (default) is the smallest the GPU offers: `1g.10gb` on H100 and A100 80GB, `1g.5gb` on A100 40GB. Others: `1g.20gb` (4 per 80GB GPU), `2g.20gb` (3), `3g.40gb` (2) |
| `labs` | How many labs to start. `mig`: 0 = one per slice. `shared`: required. At most 99 |
| `image` | Docker image per lab (default `quay.io/jupyter/pytorch-notebook:cuda12-latest`) |
| `tls` | `letsencrypt` (default), `internal` (self-signed) or `none` (plain http: tokens travel in clear) |
| `domain` | Name the labs are served on (default `<ip>.sslip.io`) |
| `hostname` | Host name of the VM, shown in every shell prompt. Default: the launch `--name`, so name launches after the event |
| `login_user` | Shared shell user (default `labagent`, empty = none) |
| `login_sudo` | Give that user sudo and docker (default true) |

## Containers: gpu-notebooks-container

`gpu-notebooks` needs a VM with root, Docker and (for isolation) MIG. A RunPod pod is a container: no Docker daemon, no MIG, and a second JupyterLab installed inside it is fragile. `gpu-notebooks-container` installs nothing. It creates the pod with **RunPod's own JupyterLab** switched on and a password it chose, then reports the URL RunPod gives that Jupyter and the password. One pod, one JupyterLab, the whole GPU. Same launch and destroy, same key handling, and a failed launch still deletes the pod it rented.

```
labagent compute offers --cloud runpod --gpu H100
labagent launch gpu-notebooks-container --name demo --param cloud=runpod \
  --param 'instance_type=NVIDIA H100 NVL' --param vendor=runpod-secure --param region=US-KS-2 \
  --param ssh_key_file=$HOME/.labagent/keys/shadeform_ed25519
labagent destroy gpu-notebooks-container --name demo --param cloud=runpod
```

- **As a file:** [`examples/launch.runpod.example.yaml`](../examples/launch.runpod.example.yaml) holds this launch (`labagent launch -f ...`, and `destroy -f` with the same file).
- **The URL** is RunPod's proxy: `https://<pod id>-8888.proxy.runpod.net` (seen to answer on a real pod, 2026-11-19). The password is `JUPYTER_PASSWORD`, passed when the pod is created and shown once in the report; it is also in a private file (`~/.labagent/packs/gpu-notebooks-container/<name>-credentials.csv`). If RunPod's Jupyter asks for a token instead of a password, use the same value.
- **Only clouds that start their own JupyterLab** are accepted (`Provider.builtin_jupyter`): today RunPod. For VMs (Lambda, Nebius, Shadeform) use `gpu-notebooks`.
- **Parameters:** `cloud` (default `runpod`), `instance_type`, `vendor`, `region`, `location`, `max_price`, `os` (a Docker image; default `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`, RunPod's official image), `ssh_key_id`, `ssh_key_file`, `keep_on_failure`, `hours`, `show_passwords`.
- **Use a different `--name` than any `gpu-notebooks` launch.** Both packs name the machine and its credentials file from `--name`, so the same name in both would make one adopt or overwrite the other's.
- **A pod bills until you destroy it.** RunPod has no auto-delete, and `hours` only sets the planned end shown in the report.
- **Not yet proven** (as of 2026-11-19): that a pod created this way has Jupyter running and accepts the password. The docs do not say whether the login is a password or a token.

## GPUs

| GPU | `mode` | Labs | Notes |
|---|---|---|---|
| H100 80GB | `mig` | 7 per GPU, 9.75 GiB each | Verified on one PCIe card. |
| A100 80GB | `mig` | 7 per GPU, same slices as H100 | Not verified on real hardware yet. |
| A100 40GB | `mig` | 7 per GPU, 4.75 GiB each (`1g.5gb`) | Not verified on real hardware yet. `mig_profile: auto` picks the size: the H100 default `1g.10gb` does not exist on a 40GB card. |
| RTX 5090, RTX 4090 | `shared` | Your choice, `labs=N` | No MIG, so no isolation. Not verified on real hardware yet. |

Find what is available and what it costs with `labagent compute offers --cloud shadeform --gpu A100` (families: `A100` is the 40GB card, `A100_80G` the 80GB one).

**A100: a reboot may happen.** On an A100, turning MIG on can need a GPU reset, which a VM often cannot do. The pack tries the reset, and if MIG is still not active it reboots the VM once, waits for it (up to 10 minutes), and carries on. You will see `rebooting the VM once` in the output. A second request for a reboot is a failure: destroy the VM and try another offer.

**RTX 5090 and other GPUs without MIG.** A 5090 has 32 GB and no MIG. `mode=mig` is refused before anything is rented. `mode=shared` starts `labs` containers that each see the whole GPU, taken in turn when there are several GPUs. There is **no isolation**: no memory limit and no fair share, so one learner who loads a large model can make everyone else's notebook fail with an out-of-memory error. Keep `labs` small (4 labs is about 8 GB each if everyone behaves) and tell learners to size their work, for example `torch.cuda.set_per_process_memory_fraction(0.25)`. Use `mig` when learners must not affect each other.

```bash
labagent launch gpu-notebooks --name rtx-workshop --param instance_type=RTX5090 --param vendor=<vendor> --param region=<region> \
    --param os=<os from labagent compute offers> --param mode=shared --param labs=4
```

## The shared login

The user gets a random 20 character password (in `<name>-ssh-login.csv`, 0600, and in the final report), ssh password login for that one user only (every other user stays key-only), your ssh key too, and a prompt that shows `user@host`. A user someone created by hand is repaired: shell, home directory, prompt.

**With sudo this is root.** Anyone with the password can read every lab's token, stop other labs and change the MIG slices, which defeats the one-container-per-slice isolation. For students, give out the lab URLs and set `login_sudo=false`, or keep the shell login for instructors.

## Cost and safety

`launch` starts a billed VM and prints the hourly price first. **If a launch fails after the VM exists** (it never becomes active, ssh cannot log in, MIG or a lab fails, or you press Ctrl-C), labagent deletes that VM before it returns, and says so in `summary.cleanup`: `{"action": "deleted", "host_id": "..."}`. If the delete itself fails it says `"action": "failed"` and how to delete the VM by hand, and keeps the state file so `destroy` still works. Two cases are left running on purpose: `keep_on_failure=yes`, and a lab that was already working before a failed rerun (`"action": "kept"`). A killed process (SIGKILL, power loss) cannot clean up: on Shadeform the cloud still deletes the VM after `hours`, on Lambda and Nebius nothing does, so check their console after a crash. `destroy` deletes only the VM this launch created: the id in the state file, or after a crash a VM with exactly this name and tag, never by prefix. Tokens go to the VM on ssh stdin, never on a command line, and are not in state or the result JSON.

## Verified on a real VM (2026-11-13)

One H100 PCIe on Scaleway through Shadeform (`ubuntu24.04_cuda13.0_shade_os`, driver 580, Docker and the NVIDIA container toolkit preinstalled): launch, 7 slices, 7 JupyterLab containers behind a Let's Encrypt certificate, token enforcement, password login for the shared user, the prompt, and destroy. Seven PyTorch containers on seven slices ran at once with no slowdown from each other.

**Nebius: one real launch, 2026-11-14.** One H100 (`gpu-h100-sxm/1gpu-16vcpu-200gb`, `ubuntu24.04-cuda13.0`) in a project in `eu-north1`, logged in through the `nebius` command line tool: the create call was accepted as written, the VM was ready for the installer, MIG made 7 slices of `1g.10gb`, 7 JupyterLab containers came up behind a Let's Encrypt certificate, every lab answered, needed its token and saw one slice, and from outside the HTTPS front door answered (a redirect, 302, for a request with no token). The whole launch took about 5 minutes. `destroy` deleted the VM and its boot disk (the project listed no instance and no disk a minute later). Still open: the other two login ways (token, service account) were not run against the real API; the list prices in [compute.md](compute.md#nebius) were not compared with a real Nebius bill; quota or capacity errors, a failed launch's cleanup and other regions were not exercised; the API gave no GPU memory, so the card's known memory is used (H100 80, H200 141, B200 unknown). Besides that, everything listed for Lambda below that is not specific to Lambda (reboot, A100, shared mode) is open here too.

**Lambda Cloud is not verified on a real VM.** Its API calls were checked against Lambda's published API description and read-only calls to a real account (offers, SSH keys, instances, firewall rules), and the provider and the pack on top of it are tested against a stand-in for the API. Nobody has yet launched a real Lambda VM with this pack, so these are open: that Docker and its NVIDIA runtime are on the Lambda Stack image (the installer stops with a plain message if not, and the failed launch deletes the VM), that MIG can be enabled inside a Lambda VM, and that the 80 and 443 firewall ruleset lets Let's Encrypt and the labs through.

**Not verified yet:** A100 (both sizes), the RTX 5090 shared mode and the A100 reboot path (they are tested against a stand-in for the GPU driver, not real cards), a reboot of the VM (NVIDIA says Hopper does not keep MIG, so the boot unit rebuilds it; that unit has not been rebooted), the 8-GPU SXM shape, and many learners using the labs at once. Shadeform capacity changes within minutes: an offer that passed the preflight can be gone a moment later, and nothing is created when it is.

**MIG limit.** An H100 splits into at most 7 slices, so an 8-GPU box gives 56 labs, not 64.

**Adding a cloud.** Subclass `Provider` in `labagent/compute/providers.py` (offers, ssh_keys, validate, find, create, get, delete) and add it to `PROVIDERS`: its key becomes a value of `--param cloud`.
