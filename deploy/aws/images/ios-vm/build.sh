#!/bin/bash
# Build an iOS environment's Tart VM image. Run on a Mac host as the VM owner inside its GUI session.
#   XCODE_VERSION=<x.y> build.sh [--constraints <file>] <base-vm> <vm-name> <private-key-file> <base-installs.json> [<type-installs.json>]
#
# <base-vm> is a local Tart VM, cloned from the environment type's base_image,
# that already trusts <private-key-file>.pub. Packer's SSH client stalls against
# the guest's OpenSSH, so this uses ssh and scp instead. The installs files hold
# {name, version, url, sha256} lists, as for base.pkr.hcl. --constraints is a hashed
# pip constraints file that install.sh uses for the armature wheel. Push the result with
# `tart push <vm-name> <ImageRepositoryUri>:<tag>` and set that as the type's image.
set -euo pipefail

cpu_count="${CPU_COUNT:-4}"
memory_gb="${MEMORY_GB:-10}"
boot_wait=600
# Outside /private/tmp, which lockdown.sh scans for credential patterns its own text contains.
remote_dir=/private/var/tmp/armature-build

die() { echo "ios-vm build: $*" >&2; exit 1; }

constraints=""
if [ "${1:-}" = --constraints ]; then
  constraints="${2:-}"
  [ -f "$constraints" ] || die "missing constraints file $constraints"
  shift 2
fi
[ $# -ge 4 ] && [ $# -le 5 ] || die "usage: XCODE_VERSION=<x.y> $0 [--constraints <file>] <base-vm> <vm-name> <private-key-file> <base-installs.json> [<type-installs.json>]"
[ -n "${XCODE_VERSION:-}" ] || die "set XCODE_VERSION to the Xcode the image must hold"
base_vm="$1"
vm="$2"
key="$3"
base_installs="$4"
type_installs="${5:-}"
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$key" ] && [ -f "$key.pub" ] || die "missing $key or $key.pub"
[ -f "$base_installs" ] || die "missing $base_installs"
[ -z "$type_installs" ] || [ -f "$type_installs" ] || die "missing $type_installs"
for f in verify.sh lockdown.sh ../install.sh; do
  [ -f "$here/$f" ] || die "$f is missing next to $0"
done

ssh_opts=(-i "$key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ConnectTimeout=10 -o LogLevel=ERROR)
ip=""
pid=""
built=false

# shellcheck disable=SC2029
vm_ssh() { ssh "${ssh_opts[@]}" "admin@$ip" "$@"; }
vm_scp() { scp -q "${ssh_opts[@]}" "$1" "admin@$ip:$2"; }
step() { echo "ios-vm build: $*"; }
# run_inline NAME: uploads stdin as a script and runs it, so nothing in it can read the rest.
run_inline() { vm_ssh "cat > $remote_dir/$1.sh" && vm_ssh "bash $remote_dir/$1.sh" </dev/null; }

cleanup() {
  [ "$built" = false ] || return 0
  tart stop "$vm" >/dev/null 2>&1 || true
  [ -z "$pid" ] || wait "$pid" 2>/dev/null || true
  tart delete "$vm" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "cloning $base_vm to $vm"
tart delete "$vm" >/dev/null 2>&1 || true
tart clone "$base_vm" "$vm"
tart set "$vm" --cpu "$cpu_count" --memory "$(( memory_gb * 1024 ))"
tart run --no-graphics "$vm" &
pid=$!

step "waiting for SSH"
deadline=$(( $(date +%s) + boot_wait ))
until ip="$(tart ip "$vm" 2>/dev/null)" && [ -n "$ip" ] && vm_ssh true </dev/null 2>/dev/null; do
  kill -0 "$pid" 2>/dev/null || die "the VM exited while booting"
  [ "$(date +%s)" -lt "$deadline" ] || die "no SSH login within ${boot_wait}s"
  sleep 5
done
vm_ssh "mkdir -p $remote_dir" </dev/null

step "installing tools"
vm_scp "$here/../install.sh" "$remote_dir/install.sh"
vm_scp "$base_installs" "$remote_dir/base-installs.json"
if [ -n "$type_installs" ]; then
  vm_scp "$type_installs" "$remote_dir/type-installs.json"
else
  printf '[]\n' | vm_ssh "cat > $remote_dir/type-installs.json"
fi
# An empty file means no constraints, as in base.pkr.hcl.
if [ -n "$constraints" ]; then
  vm_scp "$constraints" "$remote_dir/pip-constraints.txt"
else
  vm_ssh "cat > $remote_dir/pip-constraints.txt" </dev/null
fi
run_inline tools <<'EOF'
set -eux
eval "$(/opt/homebrew/bin/brew shellenv)"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
brew install coreutils jq python@3.12
ln -sf "$(brew --prefix)/bin/gtimeout" "$(brew --prefix)/bin/timeout"
ln -sf "$(brew --prefix python@3.12)/bin/python3.12" "$(brew --prefix)/bin/python3"
build=/private/var/tmp/armature-build
constraints=()
if [ -s "$build/pip-constraints.txt" ]; then constraints=(--constraints "$build/pip-constraints.txt"); fi
sudo env PATH="$PATH" bash "$build/install.sh" ${constraints[@]+"${constraints[@]}"} "$build/base-installs.json"
sudo env PATH="$PATH" bash "$build/install.sh" ${constraints[@]+"${constraints[@]}"} "$build/type-installs.json"
EOF

step "verifying"
vm_scp "$here/verify.sh" "$remote_dir/verify.sh"
vm_ssh "XCODE_VERSION=$(printf '%q' "$XCODE_VERSION") bash -l $remote_dir/verify.sh" </dev/null

step "cleaning up"
run_inline cleanup <<'EOF'
set -eux
rm -rf /Users/admin/Library/Caches/Homebrew /Users/admin/Library/Developer/Xcode/DerivedData
rm -rf /Users/admin/.zsh_history /Users/admin/.bash_history /Users/admin/.zsh_sessions /Users/admin/.bash_sessions
df -h /
EOF

step "locking down"
vm_scp "$here/lockdown.sh" "$remote_dir/lockdown.sh"
vm_ssh "AUTHORIZED_KEY=$(printf '%q' "$(cat "$key.pub")") bash -l $remote_dir/lockdown.sh" </dev/null
vm_ssh "rm -rf $remote_dir" </dev/null
vm_ssh true </dev/null || die "the shared key no longer logs in after lockdown"

step "stopping $vm"
tart stop --timeout 120 "$vm"
wait "$pid" || true
pid=""
built=true
step "built $vm"
