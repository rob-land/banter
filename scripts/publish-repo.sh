#!/usr/bin/env bash
# Build the Flatpak and publish it to the `pages` branch on Codeberg,
# which Codeberg Pages serves at https://robland.codeberg.page/banter/.
#
# Prerequisites (one-time):
#   - flatpak, flatpak-builder, ostree, rsync installed on host
#   - org.gnome.Platform//50 and org.gnome.Sdk//50 installed (--user)
#   - `pages` branch exists on origin (create with: git switch --orphan pages
#     && git commit --allow-empty -m init && git push -u origin pages)
#
# Usage:
#   scripts/publish-repo.sh [tag-or-message]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LABEL="${1:-$(git describe --tags --always --dirty)}"

echo ">> Building Flatpak (x86_64)"
flatpak-builder --user --repo=repo --force-clean _build build-aux/flatpak/land.rob.Banter.json

# Uncomment to also build aarch64 (requires qemu-user-static + binfmt registration):
# flatpak-builder --user --arch=aarch64 --repo=repo --force-clean _build-aarch64 build-aux/flatpak/land.rob.Banter.json

echo ">> Updating OSTree repo summary + pruning"
flatpak build-update-repo --prune --prune-depth=10 repo

echo ">> Preparing pages worktree"
if [ ! -d _pages ]; then
    git fetch origin pages
    git worktree add _pages pages
fi
git -C _pages pull --rebase --autostash origin pages

echo ">> Syncing OSTree repo into pages branch"
rsync -a --delete --exclude=.git repo/ _pages/
cp data/banter.flatpakrepo _pages/

cd _pages
if git diff --quiet && git diff --cached --quiet; then
    echo ">> No changes to publish"
    exit 0
fi

git add -A
git commit -m "Publish $LABEL"
git push origin pages

echo ">> Published $LABEL"
echo ">> Live at https://robland.codeberg.page/banter/"
