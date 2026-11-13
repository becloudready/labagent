#!/usr/bin/env bash
# labagent gpu-notebooks: runs ON the GPU VM, as root. The pack installs it as /opt/labagent/start-labs.sh, and a systemd unit
# runs `start-labs.sh all` at every boot: MIG slices do not survive a reboot on Hopper GPUs, so they are rebuilt each time.
#
# Subcommands: partition | access | labs | proxy | verify | install_service | all. Each one is safe to run again.
# Secrets: the lab tokens live in $CSV (0600). They reach Docker through the environment, never on a command line,
# and this script never prints them. The shared login's password arrives on stdin (`access`) and is never printed either.
set -euo pipefail

ENV_FILE=/opt/labagent/labs.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
: "${PROFILE:=1g.10gb}" "${IMAGE:=quay.io/jupyter/pytorch-notebook:cuda12-latest}" "${TLS:=letsencrypt}" "${LABS_DIR:=/root/labs}" "${DOMAIN:=}"
CSV="$LABS_DIR/credentials.csv"
PROXY=labs-proxy
CADDYFILE=/opt/labagent/Caddyfile

die() { echo "ERROR: $*" >&2; exit 1; }
slices() { nvidia-smi -L | grep -o 'MIG-[0-9a-f-]*' || true; }
lab_containers() { docker ps -aq --filter 'name=^lab-[0-9]+$'; }
gpu_indexes() { nvidia-smi --query-gpu=index --format=csv,noheader; }
have_image() { docker image inspect "$1" >/dev/null 2>&1 || docker pull -q "$1" >/dev/null || die "cannot pull $1"; }

wait_gpu() {
  local _
  for _ in $(seq 1 60); do nvidia-smi -L >/dev/null 2>&1 && return 0; sleep 2; done
  die "nvidia-smi does not answer: is the NVIDIA driver installed?"
}

partition() {
  wait_gpu
  command -v docker >/dev/null || die "docker is not installed on this VM"
  docker info 2>/dev/null | grep -qi nvidia || die "the NVIDIA container runtime is not set up in Docker"
  local g _ have wrong
  for g in $(gpu_indexes); do
    if [ "$(nvidia-smi -i "$g" --query-gpu=mig.mode.current --format=csv,noheader)" != "Enabled" ]; then
      nvidia-smi -i "$g" -mig 1 >/dev/null || die "cannot enable MIG on GPU $g: this VM may not allow it"
    fi
  done
  have=$(slices | wc -l)
  wrong=$(nvidia-smi -L | grep 'MIG ' | grep -vc "MIG $PROFILE " || true)
  if [ "$have" -eq 0 ] || [ "$wrong" -gt 0 ]; then
    lab_containers | xargs -r docker rm -f >/dev/null
    nvidia-smi mig -dci >/dev/null 2>&1 || true
    nvidia-smi mig -dgi >/dev/null 2>&1 || true
    for g in $(gpu_indexes); do
      for _ in $(seq 1 16); do nvidia-smi mig -i "$g" -cgi "$PROFILE" -C >/dev/null 2>&1 || break; done
    done
  fi
  have=$(slices | wc -l)
  [ "$have" -gt 0 ] || die "no MIG slice of profile $PROFILE could be created (valid profiles: nvidia-smi mig -lgip)"
  echo "SLICES=$have"
  echo "PROFILE=$PROFILE"
  echo "GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  echo "GPU_COUNT=$(gpu_indexes | wc -l)"
}

access() {
  local pw src home
  IFS= read -r pw || die "no password on stdin"
  [ "${#pw}" -ge 16 ] || die "the password is too short"
  if [ -n "${LAB_HOSTNAME:-}" ]; then
    hostnamectl set-hostname "$LAB_HOSTNAME" || die "cannot set the hostname"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $LAB_HOSTNAME/" /etc/hosts; else echo "127.0.1.1 $LAB_HOSTNAME" >> /etc/hosts; fi
    # cloud-init would put the provider's name back at the next boot
    if [ -d /etc/cloud/cloud.cfg.d ]; then echo "preserve_hostname: true" > /etc/cloud/cloud.cfg.d/99-labagent-hostname.cfg; fi
  fi
  [ -n "${LOGIN_USER:-}" ] || return 0
  if id "$LOGIN_USER" >/dev/null 2>&1; then
    usermod -s /bin/bash "$LOGIN_USER"      # an account made by hand may have /bin/sh and no home
  else
    useradd -m -s /bin/bash "$LOGIN_USER" || die "cannot create the user $LOGIN_USER"
  fi
  echo "$LOGIN_USER:$pw" | chpasswd || die "cannot set the password for $LOGIN_USER"
  if [ "${LOGIN_SUDO:-yes}" = "yes" ]; then
    usermod -aG sudo "$LOGIN_USER"
    getent group docker >/dev/null && usermod -aG docker "$LOGIN_USER"
  else
    gpasswd -d "$LOGIN_USER" sudo >/dev/null 2>&1 || true
    gpasswd -d "$LOGIN_USER" docker >/dev/null 2>&1 || true
  fi
  home=$(getent passwd "$LOGIN_USER" | cut -d: -f6)
  grp=$(id -gn "$LOGIN_USER")
  install -d -m 755 -o "$LOGIN_USER" -g "$grp" "$home"      # repairs a home that is missing or owned by root
  for f in .bashrc .profile .bash_logout; do
    [ -e "$home/$f" ] || install -m 644 -o "$LOGIN_USER" -g "$grp" "/etc/skel/$f" "$home/$f" 2>/dev/null || true
  done
  src="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/.ssh/authorized_keys"
  if [ -r "$src" ]; then      # the instructor's key works for this user too
    install -d -m 700 -o "$LOGIN_USER" -g "$grp" "$home/.ssh"
    install -m 600 -o "$LOGIN_USER" -g "$grp" "$src" "$home/.ssh/authorized_keys"
  fi
  # the prompt shows user@host (colour), for login shells (.bash_profile) and every interactive shell (.bashrc). Managed block: safe to rerun.
  touch "$home/.bashrc"
  sed -i '/# >>> labagent prompt >>>/,/# <<< labagent prompt <<</d' "$home/.bashrc"
  cat >> "$home/.bashrc" <<'RC'
# >>> labagent prompt >>>
PS1='\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '
# <<< labagent prompt <<<
RC
  cat > "$home/.bash_profile" <<'RC'
# labagent: login shells read .profile (PATH and friends) and then .bashrc (the prompt)
[ -f "$HOME/.profile" ] && . "$HOME/.profile"
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
RC
  chown "$LOGIN_USER:$grp" "$home/.bashrc" "$home/.bash_profile"
  # prove it: a real login shell of that user must end up with our prompt, not just a file that says so
  runuser -u "$LOGIN_USER" -- bash -lic 'printf %s "$PS1"' 2>/dev/null | grep -qF '\e[1;32m' || die "the prompt of $LOGIN_USER is not set: something else sets PS1 after ~/.bashrc"
  # password login for this one user only: every other user stays key-only
  printf 'Match User %s\n    PasswordAuthentication yes\n' "$LOGIN_USER" > /etc/ssh/sshd_config.d/01-labagent-login.conf
  sshd -t || { rm -f /etc/ssh/sshd_config.d/01-labagent-login.conf; die "sshd rejected the login settings"; }
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || die "cannot reload sshd"
  [ "$(sshd -T -C "user=$LOGIN_USER,host=localhost,addr=127.0.0.1" | awk '$1=="passwordauthentication"{print $2}')" = "yes" ] ||
    die "password login for $LOGIN_USER is still off: another sshd setting wins"
  echo "HOSTNAME=$(hostname)"
  echo "LOGIN_USER=$LOGIN_USER"
  echo "PROMPT=set"
}

labs() {
  [ -s "$CSV" ] || die "missing $CSV"
  local uuids n cpus mem lab port token idx uuid want have_env
  mapfile -t uuids < <(slices)
  n=$(( $(wc -l < "$CSV") - 1 ))
  [ "${#uuids[@]}" -ge "$n" ] || die "$n labs need $n MIG slices, found ${#uuids[@]}"
  have_image "$IMAGE"
  cpus=$(awk -v c="$(nproc)" -v n="$n" 'BEGIN { x = 2 * c / n; if (x < 1) x = 1; printf "%.1f", x }')
  mem=$(awk -v n="$n" '/MemTotal/ { x = int($2 / 1048576 * 0.8 / n); if (x < 2) x = 2; print x }' /proc/meminfo)
  idx=0
  while IFS=, read -r lab port token; do
    [ "$lab" = "lab" ] && continue
    uuid="${uuids[$idx]}"
    idx=$((idx + 1))
    want="$uuid|$token"
    if docker inspect "$lab" >/dev/null 2>&1; then
      have_env=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$lab" |
        awk -F= '$1=="NVIDIA_VISIBLE_DEVICES"{u=$2} $1=="JUPYTER_TOKEN"{t=$2} END{print u "|" t}')
      if [ "$have_env" = "$want" ] && [ "$(docker inspect -f '{{.State.Running}}' "$lab")" = "true" ]; then continue; fi
      docker rm -f "$lab" >/dev/null
    fi
    install -d -o 1000 -g 100 "$LABS_DIR/$lab"
    JUPYTER_TOKEN="$token" docker run -d --name "$lab" --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES="$uuid" -e JUPYTER_TOKEN \
      --cpus "$cpus" --memory "${mem}g" --shm-size 1g --pids-limit 4096 --security-opt no-new-privileges \
      -p "127.0.0.1:$port:8888" -v "$LABS_DIR/$lab:/home/jovyan/work" "$IMAGE" \
      start-notebook.py --ServerApp.base_url="/$lab" >/dev/null || die "cannot start $lab"
  done < "$CSV"
}

proxy() {
  [ -s "$CSV" ] || die "missing $CSV"
  [ -n "$DOMAIN" ] || die "DOMAIN is not set"
  local site tlsline='' new changed=0 lab port _
  case "$TLS" in
    letsencrypt) site="$DOMAIN" ;;
    internal) site="$DOMAIN"; tlsline='tls internal' ;;
    none) site="http://$DOMAIN" ;;
    *) die "TLS must be letsencrypt, internal or none" ;;
  esac
  new=$(mktemp)
  {
    echo "$site {"
    [ -z "$tlsline" ] || echo "  $tlsline"
    while IFS=, read -r lab port _; do
      [ "$lab" = "lab" ] && continue
      echo "  handle /$lab* {"
      echo "    reverse_proxy 127.0.0.1:$port"
      echo "  }"
    done < "$CSV"
    echo "  handle {"
    echo "    respond \"GPU labs: open your own lab link.\" 200"
    echo "  }"
    echo "}"
  } > "$new"
  have_image caddy:2
  cmp -s "$new" "$CADDYFILE" || { install -m 644 "$new" "$CADDYFILE"; changed=1; }
  rm -f "$new"
  if ! docker inspect "$PROXY" >/dev/null 2>&1; then
    docker run -d --name "$PROXY" --restart unless-stopped --network host \
      -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" -v labagent-caddy-data:/data caddy:2 >/dev/null || die "cannot start the proxy"
  elif [ "$changed" = 1 ] || [ "$(docker inspect -f '{{.State.Running}}' "$PROXY")" != "true" ]; then
    docker restart "$PROXY" >/dev/null
  fi
}

verify() {
  local tmp lab port token
  tmp=$(mktemp -d)
  while IFS=, read -r lab port token; do
    [ "$lab" = "lab" ] && continue
    printf 'Authorization: token %s\n' "$token" > "$tmp/$lab.hdr"
    (
      local url="http://127.0.0.1:$port/$lab/api" _ up anon with gpu
      for _ in $(seq 1 90); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$url")" = "200" ] && break
        sleep 2
      done
      up=$(curl -s -o /dev/null -w '%{http_code}' "$url")
      anon=$(curl -s -o /dev/null -w '%{http_code}' "$url/contents")
      with=$(curl -s -o /dev/null -w '%{http_code}' -H "@$tmp/$lab.hdr" "$url/contents")
      gpu=$(docker exec "$lab" python -c 'import torch; p = torch.cuda.get_device_properties(0); print(torch.cuda.device_count(), p.name, p.total_memory // 2**20)' 2>&1 | tail -1)
      echo "LAB $lab up=$up anon=$anon token=$with | $gpu" > "$tmp/$lab.out"
    ) &
  done < "$CSV"
  wait
  cat "$tmp"/lab-*.out 2>/dev/null || true
  rm -rf "$tmp"
}

install_service() {
  cat > /etc/systemd/system/labagent-labs.service <<'UNIT'
[Unit]
Description=labagent GPU labs: MIG slices and JupyterLab containers
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=900
ExecStart=/opt/labagent/start-labs.sh all

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable labagent-labs.service >/dev/null 2>&1
}

case "${1:-}" in
  partition | access | labs | proxy | verify | install_service) "$1" ;;
  all) partition >/dev/null; labs; proxy ;;
  *) die "usage: $0 partition|access|labs|proxy|verify|install_service|all" ;;
esac
