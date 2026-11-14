# Launch files

A launch file is a YAML description of one pack run, so a long command line becomes a file you can keep in git, review and reuse.

```yaml
# hackathon.yaml
pack: gpu-notebooks
name: nvidia-h100-workshop
params:
  vendor: scaleway
  region: paris-france-1
  ssh_key_id: ${SHADEFORM_SSH_KEY_ID}
  hours: 4
  login_sudo: false
```

```bash
labagent launch  -f hackathon.yaml --dry-run     # show the steps, create nothing
labagent launch  -f hackathon.yaml
labagent destroy -f hackathon.yaml               # the same file removes what it created
labagent launch  -f hackathon.yaml --param hours=2 --name quick-test      # flags win over the file
```

| Key | Meaning |
|---|---|
| `pack` | Pack id or alias. Optional if you give it on the command line. If both are given they must be the same pack |
| `name` | The instance name (state key, resource names). Default: `--name`, else the pack's own default |
| `params` | Pack parameters, the same names as `--param`. `labagent packs --json` lists them |
| `description` | Free text for people. Ignored |

Any other top-level key is an error, so a typo such as `param:` is caught instead of silently ignored. A misspelled parameter name is caught the same way as on the command line.

**It behaves exactly like the flags.** Every value becomes the string the command line would have given: `hours: 6` is `6`, `true` is `true`, a list is a comma list (`vms: [a, b]` is `a,b`). A key with no value means "use the pack's default". Only `true` and `false` are booleans: `yes`, `no`, `on` and `off` stay text, so `apply: yes` means what `--param apply=yes` means. A plain value never needs quotes unless it would look like something else (`'1.10'`).

**Secrets stay out of the file.** `${NAME}` is replaced from the environment, `${NAME:-fallback}` uses the fallback when `NAME` is unset or empty, and a missing variable with no fallback stops the run and names the variable. Keep tokens and keys in the environment or in `~/.labagent/credentials`, not in a file you commit.

The file is read with a safe YAML loader (no code runs) and must be UTF-8 text under 1 MB.

Example: [docs/examples/gpu-notebooks-hackathon.yaml](examples/gpu-notebooks-hackathon.yaml).
