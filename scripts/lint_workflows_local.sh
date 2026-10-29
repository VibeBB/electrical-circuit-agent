#!/usr/bin/env bash
# Run the same actionlint + zizmor checks as .github/workflows/workflow-lint.yml
# locally before pushing workflow edits. Downloads are pinned to the same
# versions and sha256 digests as the workflow; binaries cache under
# $XDG_CACHE_HOME/vibebb-workflow-lint (or ~/.cache/vibebb-workflow-lint).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

tooldir="${XDG_CACHE_HOME:-$HOME/.cache}/vibebb-workflow-lint"
mkdir -p "$tooldir"

# actionlint (same pin and sha256 as workflow-lint.yml, from
# actionlint_1.7.12_checksums.txt on the v1.7.12 release).
tarball="actionlint_1.7.12_linux_amd64.tar.gz"
if [ ! -x "$tooldir/actionlint" ]; then
  curl --fail --location --silent --show-error --retry 8 --retry-delay 20 \
    --retry-max-time 600 --retry-all-errors \
    --output "$tooldir/$tarball" \
    "https://github.com/rhysd/actionlint/releases/download/v1.7.12/$tarball"
  echo "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8  $tooldir/$tarball" \
    | sha256sum -c -
  tar -xzf "$tooldir/$tarball" -C "$tooldir" actionlint
fi
"$tooldir/actionlint" -oneline

# zizmor (same pinned wheel and sha256 as workflow-lint.yml).
wheel="zizmor-1.31.1-py3-none-manylinux_2_28_x86_64.whl"
if [ ! -f "$tooldir/$wheel" ]; then
  curl --fail --location --silent --show-error --retry 8 --retry-delay 20 \
    --retry-max-time 600 --retry-all-errors \
    --output "$tooldir/$wheel" \
    "https://files.pythonhosted.org/packages/7c/f7/ee2c8eedc7bf07f443a41aa12f828c7846fd17bab184c22403afd0cbb137/$wheel"
  echo "58e878eb2dbd2c6183c105e1ac28c14f7dd1eca44a783f1fe2a69647d012c410  $tooldir/$wheel" \
    | sha256sum -c -
fi
# Online audits (advisory DB lookups, ref resolution) activate when GH_TOKEN
# is set in the caller's environment; without it zizmor runs its offline set.
uvx --from "$tooldir/$wheel" zizmor .github/workflows
