#!/bin/bash
# Last build step: key-only SSH and a random admin password that only
# /etc/kcpassword knows, so auto-login (which the simulator needs) still works.
# build.sh runs it inside the VM with AUTHORIZED_KEY. Never echo the password.
set -euo pipefail

die() { echo "lockdown: $*" >&2; exit 1; }

[ -n "${AUTHORIZED_KEY:-}" ] || die "AUTHORIZED_KEY is empty"

install -d -m 700 "$HOME/.ssh"
printf '%s\n' "$AUTHORIZED_KEY" > "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"

# sshd keeps the first value it reads, and this file sorts before 100-macos.conf.
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
  | sudo tee /etc/ssh/sshd_config.d/010-armature.conf >/dev/null
sudo sshd -T | grep -qx 'passwordauthentication no' || die "sshd still allows passwords"

sudo launchctl disable system/com.apple.screensharing
sudo launchctl print-disabled system | grep -Eq '"com.apple.screensharing" => (disabled|true)' \
  || die "Screen Sharing is still enabled"

pw="$(openssl rand -hex 24)"
sudo sysadminctl -resetPasswordFor admin -newPassword "$pw" -adminUser admin -adminPassword admin
dscl /Local/Default -authonly admin "$pw" || die "password reset failed"
if [ -f "$HOME/Library/Keychains/login.keychain-db" ]; then
  security set-keychain-password -o admin -p "$pw" "$HOME/Library/Keychains/login.keychain-db"
fi

key=(125 137 82 35 210 188 221 234 163 185 31)
read -r -a bytes <<< "$(printf '%s' "$pw" | od -An -v -tu1 | tr '\n' ' ')"
bytes+=(0)
while (( ${#bytes[@]} % 12 )); do bytes+=(0); done
encoded=""
for i in "${!bytes[@]}"; do
  printf -v hex '\\x%02x' $(( bytes[i] ^ key[i % 11] ))
  encoded+="$hex"
done
unset pw
# shellcheck disable=SC2059
printf "$encoded" | sudo tee /etc/kcpassword >/dev/null
sudo chmod 600 /etc/kcpassword
sudo chown root:wheel /etc/kcpassword
sudo defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser admin

pattern='AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key"?[[:space:]]*[=:][[:space:]]*"?[A-Za-z0-9/+]{40}|AWS_SECRET_ACCESS_KEY=[A-Za-z0-9/+]{40}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|AWS_BEARER_TOKEN_BEDROCK"?[[:space:]]*[=:][[:space:]]*"?[A-Za-z0-9+/=]{20,}|ABSK[A-Za-z0-9+/=]{20,}'
# Built at run time so this script's own text never matches.
probe="$(mktemp)"
printf 'AKIA%s\n' "$(printf 'X%.0s' $(seq 16))" > "$probe"
printf '"AWS_BEARER_TOKEN_%s": "%s"\n' BEDROCK "$(printf 'Y%.0s' $(seq 24))" >> "$probe"
[ "$(grep -cE "$pattern" "$probe")" -eq 2 ] || die "the credential pattern missed a known key shape"
rm -f "$probe"
rc=0
hits="$(sudo grep -rIoHE -D skip "$pattern" "$HOME" /private/tmp /usr/local/share /etc/ssh)" || rc=$?
# AWS documentation sample keys (…EXAMPLE, …EXAMPLEKEY) ship inside SDK gems.
leaks="$(printf '%s\n' "$hits" | grep -v EXAMPLE | cut -d: -f1 | sort -u | grep . || true)"
[ "$rc" -ne 2 ] || die "the credential scan could not read every file"
[ -z "$leaks" ] || die "credential patterns in: $leaks"
