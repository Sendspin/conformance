#!/bin/sh
# Install the sendspin-validate command on Linux or macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/Sendspin/conformance/main/scripts/install-sendspin-validate.sh | sh
#
# Installs uv first when it is missing; uv fetches a suitable Python itself. The
# validator's dependencies are pinned to the versions uv.lock records, so a release
# upstream cannot change what a run does. Run it again to update.
#
# SENDSPIN_VALIDATE_REF picks the branch, tag or commit to install (default: main).
set -eu

ref="${SENDSPIN_VALIDATE_REF:-main}"
source_url="https://github.com/Sendspin/conformance/archive/$ref.tar.gz"
constraints_url="https://raw.githubusercontent.com/Sendspin/conformance/$ref/scripts/sendspin-validate-constraints.txt"

say() { printf '%s\n' "$*"; }
fail() {
    say "error: $*" >&2
    exit 1
}

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (https://docs.astral.sh/uv/), which runs the validator..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "neither curl nor wget is installed; install one, or uv itself, and run this again"
    fi
    # The uv installer adds its directory to PATH for new shells only.
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv is installed but not on PATH; open a new terminal and run this again"
fi

say "Installing sendspin-validate from $ref..."
# A source archive rather than git+https, so a machine without git can install it.
uv tool install --force --constraints "$constraints_url" "sendspin-conformance @ $source_url"

bin_dir="$(uv tool dir --bin)"
case ":$PATH:" in
    *":$bin_dir:"*)
        say ""
        say "Done. Run: sendspin-validate"
        ;;
    *)
        say ""
        say "Done. $bin_dir is not on your PATH yet: run 'uv tool update-shell', open a new"
        say "terminal, then run: sendspin-validate"
        ;;
esac
