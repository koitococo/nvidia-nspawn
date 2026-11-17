#!/usr/bin/env bash
set -euo pipefail

# Run inside the matching distribution, with its native RPM/Python build tools.
distro=${1:?Usage: build-rpm.sh fedora45|opensuse-leap16 OUTPUT_DIRECTORY}
output=${2:?Usage: build-rpm.sh fedora45|opensuse-leap16 OUTPUT_DIRECTORY}
: "${TAG_NAME:?Set TAG_NAME to the release tag (vX.Y.Z)}"

source /etc/os-release
case "$distro:$ID:$VERSION_ID" in
  fedora45:fedora:45) dist_suffix=.fc45 ;;
  opensuse-leap16:opensuse-leap:16.0) dist_suffix=.lp160 ;;
  *) printf 'Unsupported build environment: %s (%s %s)\n' "$distro" "$ID" "$VERSION_ID" >&2; exit 1 ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."
version=$(python3 - <<'PY'
import os
from pathlib import Path
import re
import tomllib

version = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
    raise SystemExit('Release versions must use X.Y.Z')
if os.environ['TAG_NAME'] != 'v' + version:
    raise SystemExit(f'Tag must be v{version}, not {os.environ["TAG_NAME"]!r}')
spec = Path('packaging/nvidia-nspawn.spec').read_text()
match = re.search(r'^Version:\s*(\S+)\s*$', spec, re.MULTILINE)
if match is None or match.group(1) != version:
    raise SystemExit('RPM spec Version must match pyproject.toml')
print(version)
PY
)

mkdir -p "$output"
output=$(realpath "$output")
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
mkdir -p "$workdir/SOURCES"
python3 - "$workdir/SOURCES" <<'PY'
import sys
from setuptools.build_meta import build_sdist
build_sdist(sys.argv[1])
PY

rpmbuild -ba \
  --define "_topdir $workdir" \
  --define "dist $dist_suffix" \
  packaging/nvidia-nspawn.spec

# Publish only this build's binary and source RPM, never an older dist/ file.
shopt -s nullglob
packages=("$workdir"/RPMS/noarch/*.rpm "$workdir"/SRPMS/*.src.rpm)
if [[ ${#packages[@]} -ne 2 ]]; then
  printf 'Expected one binary RPM and one source RPM; got %s\n' "${#packages[@]}" >&2
  exit 1
fi
cp "${packages[@]}" "$output/"
printf 'Built %s for %s:\n' "$version" "$distro"
printf '  %s\n' "${packages[@]##*/}"
