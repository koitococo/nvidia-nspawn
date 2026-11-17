#!/usr/bin/env bash
set -euo pipefail
set +x

assets=${1:?Usage: sign-rpms.sh ASSET_DIRECTORY}
: "${RPM_SIGNING_PRIVATE_KEY:?Missing RPM_SIGNING_PRIVATE_KEY}"
: "${RPM_SIGNING_PASSPHRASE:?Missing RPM_SIGNING_PASSPHRASE}"
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
public_key=$repo_root/packaging/keys/nvidia-nspawn.asc
assets=$(realpath "$assets")

workdir=$(mktemp -d)
export GNUPGHOME=$workdir/gnupg
mkdir -m 0700 "$GNUPGHOME"
cleanup() {
  gpgconf --kill gpg-agent || true
  rm -rf "$workdir"
}
trap cleanup EXIT
umask 077
printf '%s\n' "$RPM_SIGNING_PASSPHRASE" > "$GNUPGHOME/passphrase"
printf '%s\n' "$RPM_SIGNING_PRIVATE_KEY" | gpg --batch --import
unset RPM_SIGNING_PRIVATE_KEY RPM_SIGNING_PASSPHRASE

fingerprint=$(gpg --batch --with-colons --show-keys "$public_key" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ -n "$fingerprint" ]] || { echo 'Public signing fingerprint missing' >&2; exit 1; }
# --local-user below selects this exact signing key, not another imported key.
gpg --batch --list-secret-keys "$fingerprint" > /dev/null

cat > "$workdir/gpg-sign" <<'SH'
#!/bin/sh
exec gpg --batch --no-tty --pinentry-mode loopback \
  --passphrase-file "$GNUPGHOME/passphrase" "$@"
SH
chmod 0700 "$workdir/gpg-sign"

shopt -s nullglob
packages=("$assets"/*.rpm)
[[ ${#packages[@]} -eq 4 ]] || { echo 'Expected the two binary and two source RPMs' >&2; exit 1; }
# RPM 4 packages get a V4 signature by default. RPM 6 also needs a compatible
# signature when signing V6 packages for clients using RPM 4.x.
compat=()
if rpmsign --help | grep -q -- '--rpmv4'; then compat=(--rpmv4); fi
rpmsign --define "_openpgp_sign gpg" \
  --define "_openpgp_sign_id $fingerprint" \
  --define "_gpg_name $fingerprint" \
  --define "__gpg $workdir/gpg-sign" \
  --addsign "${compat[@]}" "${packages[@]}"

# Verify with a fresh RPM keyring containing only the checked-in public key.
mkdir "$workdir/rpmdb"
rpmkeys --dbpath "$workdir/rpmdb" --import "$public_key"
for package in "${packages[@]}"; do
  signature=$(rpm --dbpath "$workdir/rpmdb" -qp --qf '%{RSAHEADER:pgpsig}' "$package")
  [[ -n "$signature" && "$signature" != '(none)' ]] || { echo "Missing RSA signature: $package" >&2; exit 1; }
  rpmkeys --dbpath "$workdir/rpmdb" --checksig --verbose "$package"
done
cp "$public_key" "$assets/nvidia-nspawn-signing.asc"
