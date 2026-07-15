#!/bin/bash
# =============================================================================
# install-tmux-nosudo.sh — build a private tmux into ~/.local, NO sudo.
#
# WHY: the local-loop main runner (install-local-loop.sh) launches the dept's
# `claude --channels` session inside a tmux session (ops-loop-<slug>) so a human
# can `tmux attach` to WATCH and TYPE to the agent live. On a Mac provisioned
# WITHOUT admin rights ("M5" hosts: no sudo → no Homebrew → no tmux), that path
# was previously replaced by a `/usr/bin/script` fallback wrapper — viewable via
# `tail -f` but NOT attachable/typable. This script closes that gap by building
# tmux (+ its libevent dependency) from upstream source into $HOME/.local,
# needing only the Xcode Command Line Tools (cc/make, already present on M5).
#
# RESULT: $HOME/.local/bin/tmux (self-contained, static libevent). Then re-render
# the dept wrapper with tmux:
#   install-local-loop.sh --dept-dir <repo> --slug <slug> \
#       --tmux-bin "$HOME/.local/bin/tmux" ... --activate
#
# Idempotent-ish: re-running rebuilds. Uninstall: rm -rf ~/.local/bin/tmux and
# the ~/.local/src/{libevent,tmux}-* build dirs.
# =============================================================================
set -euo pipefail

PREFIX="${TMUX_NOSUDO_PREFIX:-$HOME/.local}"
LIBEVENT_VER="${LIBEVENT_VER:-2.1.12-stable}"
TMUX_VER="${TMUX_VER:-3.5a}"
SRC="$PREFIX/src"

log() { echo "[install-tmux-nosudo $(date +%H:%M:%S)] $*"; }

command -v cc   >/dev/null || { echo "ERR: no C compiler — run: xcode-select --install" >&2; exit 2; }
command -v make >/dev/null || { echo "ERR: no make — run: xcode-select --install" >&2; exit 2; }

mkdir -p "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/include" "$SRC"
export PATH="$PREFIX/bin:$PATH"

# ── libevent (static, no OpenSSL to keep it self-contained) ──────────────────
cd "$SRC"
log "downloading libevent $LIBEVENT_VER"
curl -fsSL -o libevent.tar.gz \
  "https://github.com/libevent/libevent/releases/download/release-${LIBEVENT_VER}/libevent-${LIBEVENT_VER}.tar.gz"
rm -rf "libevent-${LIBEVENT_VER}"; tar xzf libevent.tar.gz
cd "libevent-${LIBEVENT_VER}"
log "building libevent"
./configure --prefix="$PREFIX" --disable-openssl --disable-shared --enable-static >/dev/null
make -j4 >/dev/null
make install >/dev/null
log "libevent installed"

# ── tmux (links the static libevent above; uses the system ncurses) ──────────
cd "$SRC"
log "downloading tmux $TMUX_VER"
curl -fsSL -o tmux.tar.gz \
  "https://github.com/tmux/tmux/releases/download/${TMUX_VER}/tmux-${TMUX_VER}.tar.gz"
rm -rf "tmux-${TMUX_VER}"; tar xzf tmux.tar.gz
cd "tmux-${TMUX_VER}"
log "building tmux"
# --disable-utf8proc: tmux 3.5+ requires an explicit choice; utf8proc would be an
# extra build dep. Fine for ops use (watching/steering an agent session).
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" \
  ./configure --prefix="$PREFIX" --disable-utf8proc \
  CFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" >/dev/null
make -j4 >/dev/null
make install >/dev/null

log "done: $("$PREFIX/bin/tmux" -V) at $PREFIX/bin/tmux"
