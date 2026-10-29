#!/usr/bin/env bash
# Install each tool in a JSON list of {name, version, url, sha256}, the shape of
# environment_types.installs, and refuse any download whose sha256 differs.
#   install.sh [--constraints <file>] <installs.json> [<manifest.json>]
#
# Each file installs by its type:
#   .whl                  into /opt/armature/venv, linking its console scripts. With
#                         --constraints, pip runs with --require-hashes, so every
#                         dependency needs a pinned hash in that file.
#   .deb                  with apt
#   .tar.gz .tgz .tar.xz  under /opt/armature/tools/<name>/<version>
#   .zip                  the same
#   anything else         a single binary, /usr/local/bin/<name>
# An archive that holds one file installs it as /usr/local/bin/<name>. Otherwise
# every executable in its top level or bin/ is linked into /usr/local/bin.
# ARMATURE_INSTALL_ROOT prefixes every path, for tests.
set -euo pipefail

die() { echo "install: $*" >&2; exit 1; }

constraints=""
if [ "${1:-}" = --constraints ]; then
  constraints="${2:-}"
  [ -f "$constraints" ] || die "no such constraints file: $constraints"
  shift 2
fi
[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: $0 [--constraints <file>] <installs.json> [<manifest.json>]"
list="$1"
root="${ARMATURE_INSTALL_ROOT:-}"
manifest="${2:-$root/usr/local/share/armature-image-manifest.json}"
[ -f "$list" ] || die "no such file: $list"
command -v jq >/dev/null || die "jq is not installed"
jq -e 'type == "array" and all(.[]; type == "object")' "$list" >/dev/null || die "$list is not a JSON list of objects"

if command -v sha256sum >/dev/null; then
  sha_check() { echo "$1  $2" | sha256sum -c - >/dev/null; }
else
  sha_check() { echo "$1  $2" | shasum -a 256 -c - >/dev/null; }
fi

# Hash-checking constraints need a recent pip, so --constraints first installs this
# exact pip. The hash is PyPI's pip-26.2.2-py3-none-any.whl.
pip_version=26.2.2
pip_sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70
pip_min_major=26

tools="$root/opt/armature/tools"
venv="$root/opt/armature/venv"
bin="$root/usr/local/bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
install -d -m 0755 "$tools" "$bin" "$(dirname "$manifest")"

link_executables() {
  local dir="$1" name="$2" f
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "$dir" -mindepth 1 -maxdepth 1 -type f)
  if [ "${#files[@]}" -eq 1 ] && [ -z "$(find "$dir" -mindepth 1 -maxdepth 1 -type d)" ]; then
    chmod 0755 "${files[0]}"
    ln -sfn "${files[0]}" "$bin/$name"
    return
  fi
  while IFS= read -r f; do
    ln -sfn "$f" "$bin/$(basename "$f")"
  done < <(find "$dir" "$dir/bin" -mindepth 1 -maxdepth 1 \( -type f -o -type l \) -perm -u+x 2>/dev/null)
}

unpack() {
  local file="$1" dest="$2" inner
  rm -rf "$dest"
  install -d -m 0755 "$dest"
  case "$file" in
    *.zip) unzip -q "$file" -d "$dest" ;;
    *) tar -xf "$file" -C "$dest" ;;
  esac
  # Most releases wrap everything in one folder, so look inside it.
  inner="$dest"
  if [ "$(find "$dest" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ] && [ -d "$(find "$dest" -mindepth 1 -maxdepth 1)" ]; then
    inner="$(find "$dest" -mindepth 1 -maxdepth 1)"
  fi
  link_executables "$inner" "$3"
}

install_wheel() {
  local file="$1" sha256="$3" before after script py major
  py="$(command -v python3)" || die "python3 is not installed"
  "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "$2 needs Python 3.11 or newer"
  [ -x "$venv/bin/pip" ] || "$py" -m venv "$venv"
  before="$(ls "$venv/bin")"
  if [ -n "$constraints" ]; then
    printf 'pip==%s --hash=sha256:%s\n' "$pip_version" "$pip_sha256" > "$tmp/pip.txt"
    "$venv/bin/pip" install --quiet --no-input --require-hashes --only-binary :all: -r "$tmp/pip.txt"
    major="$("$venv/bin/pip" --version | sed -n 's/^pip \([0-9][0-9]*\)\..*/\1/p')"
    [ "${major:-0}" -ge "$pip_min_major" ] || die "the venv's pip is not $pip_min_major or newer after pinning it"
    printf '%s --hash=sha256:%s\n' "$file" "$sha256" > "$tmp/requirements.txt"
    "$venv/bin/pip" install --quiet --no-input --require-hashes -r "$tmp/requirements.txt" -c "$constraints"
  else
    "$venv/bin/pip" install --quiet --no-input "$file"
  fi
  after="$(ls "$venv/bin")"
  for script in $(comm -13 <(printf '%s\n' "$before" | sort) <(printf '%s\n' "$after" | sort)); do
    ln -sfn "$venv/bin/$script" "$bin/$script"
  done
}

i=0
# fd 3, so curl, apt, and pip can't read the list from stdin.
while IFS= read -r -u 3 entry; do
  field() { jq -r --arg k "$1" '.[$k] // empty' <<<"$entry"; }
  name="$(field name)" version="$(field version)" url="$(field url)" sha256="$(field sha256)"
  [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "entry $i: bad name '$name'"
  [[ $version =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || die "$name: bad version '$version'"
  [[ $url == https://* ]] || die "$name: the url must be https"
  [[ $sha256 =~ ^[0-9a-f]{64}$ ]] || die "$name: sha256 must be 64 lowercase hex characters"

  file="$tmp/$(basename "${url%%\?*}")"
  echo "install: $name $version"
  curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 -o "$file" "$url" || die "$name: could not download $url"
  sha_check "$sha256" "$file" || die "$name: checksum mismatch for $url"

  dest="$tools/$name/$version"
  case "$file" in
    *.whl) install_wheel "$file" "$name" "$sha256" ;;
    *.deb) DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$file" ;;
    *.tar.gz|*.tgz|*.tar.xz|*.zip) unpack "$file" "$dest" "$name" ;;
    *) install -m 0755 "$file" "$bin/$name" ;;
  esac
  chmod -R a+rX "$tools" 2>/dev/null || true
  i=$(( i + 1 ))
done 3< <(jq -c '.[]' "$list")

# Merge into the manifest, so the base and type lists both show in the image.
if [ -f "$manifest" ]; then
  jq -s '.[1] + .[0] | map({name, version, url, sha256}) | unique_by(.name)' "$manifest" "$list" > "$tmp/manifest"
else
  jq 'map({name, version, url, sha256})' "$list" > "$tmp/manifest"
fi
install -m 0644 "$tmp/manifest" "$manifest"
