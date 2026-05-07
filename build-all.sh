#!/usr/bin/env bash
# build-all.sh — build Banter flatpak bundles for x86_64 and aarch64.
#
# Usage:
#   ./build-all.sh                  # build both arches, write bundles
#   ./build-all.sh --arch x86_64    # build only one arch
#   ./build-all.sh --install        # also install host-arch bundle (--user)
#
# Outputs:
#   banter-x86_64.flatpak
#   banter-aarch64.flatpak
#
# Banter has no PyPI dependencies (urllib only), so there is no
# python3-deps.json regeneration step here. If that ever changes,
# port the --regen-deps and fix-flatpak-deps.py logic from
# clicker/build-all.sh.

set -euo pipefail

cd "$(dirname "$0")"

ARCHES=(x86_64 aarch64)
INSTALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install) INSTALL=true; shift ;;
        --arch)    ARCHES=("$2"); shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# qemu-binfmt sanity check for cross-arch builds
HOST_ARCH=$(uname -m)
needs_qemu=false
for a in "${ARCHES[@]}"; do
    [[ "$a" != "$HOST_ARCH" ]] && needs_qemu=true
done
if $needs_qemu; then
    if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 \
       && ! -e /proc/sys/fs/binfmt_misc/qemu-arm     ]]; then
        echo
        echo "Warning: cross-arch build requested but qemu binfmt is not registered."
        echo "         The aarch64 build will likely fail. Register binfmt with:"
        echo "             sudo systemctl restart systemd-binfmt"
        echo "         or:  sudo update-binfmts --enable qemu-aarch64"
        echo
    fi
fi

mkdir -p repo

for arch in "${ARCHES[@]}"; do
    builddir="_flatpak_${arch}"
    bundle="banter-${arch}.flatpak"
    echo
    echo "==== Building Banter for ${arch} ===="
    flatpak-builder --arch="$arch" --repo=repo --force-clean \
        "$builddir" build-aux/flatpak/land.rob.Banter.json
    echo "==== Bundling ${bundle} ===="
    flatpak build-bundle --arch="$arch" repo "$bundle" land.rob.Banter
    ls -lh "$bundle"
done

if $INSTALL; then
    bundle="banter-${HOST_ARCH}.flatpak"
    if [[ -f "$bundle" ]]; then
        echo
        echo "==== Installing $bundle ===="
        flatpak install --user --noninteractive --reinstall \
            --bundle "$bundle"
    else
        echo "Note: no $bundle to install (host arch not in build set)."
    fi
fi

echo
echo "Done."
