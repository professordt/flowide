#!/usr/bin/env bash
# flowide installer — Linux / macOS
# Usage (one line):  curl -fsSL https://raw.githubusercontent.com/professordt/flowide/main/install.sh | bash
# Or from a clone:   ./install.sh
set -euo pipefail

REPO_TARBALL="https://codeload.github.com/professordt/flowide/tar.gz/refs/heads/main"
DEST="$HOME/.local/share/flowide"
BIN_DIR="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
die() { printf 'flowide install: %s\n' "$*" >&2; exit 1; }

# ---- 1. Python 3.9+ -------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  die "python3 not found. Install Python 3.9 or newer and rerun."
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Flow IDE needs Python 3.9 or newer (found $("$PY" -c 'import sys;print("%d.%d" % sys.version_info[:2])'))."

# ---- 2. Locate the source tree (clone) or fetch one (curl | bash) ---------
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  if [ -n "$candidate" ] && [ -f "$candidate/pyproject.toml" ]; then
    SRC="$candidate"
  fi
fi

TMP=""
if [ -z "$SRC" ]; then
  say "[1/3] Fetching flowide source..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "$REPO_TARBALL" -o "$TMP/flowide.tar.gz" || die "could not download $REPO_TARBALL"
  tar -xzf "$TMP/flowide.tar.gz" -C "$TMP" || die "could not unpack the tarball"
  SRC="$TMP/flowide-main"
  [ -f "$SRC/pyproject.toml" ] || die "unexpected archive layout (no pyproject.toml)"
else
  say "[1/3] Using local source at $SRC"
fi

# ---- 3. Install: pip --user first, module copy fallback -------------------
installed_with_pip=0
say "[2/3] Installing..."
if "$PY" -m pip --version >/dev/null 2>&1; then
  # --no-build-isolation first so an offline box with setuptools already present still works.
  if "$PY" -m pip install --user --no-build-isolation --quiet "$SRC" >/dev/null 2>&1 \
     || "$PY" -m pip install --user --quiet "$SRC" >/dev/null 2>&1; then
    installed_with_pip=1
  fi
fi

mkdir -p "$DEST" "$BIN_DIR"

if [ "$installed_with_pip" -eq 1 ]; then
  say "      installed via pip --user"
else
  say "      pip unavailable or refused (PEP 668 / offline) -> copying modules"
  cp -R "$SRC/src/flowide/." "$DEST/flowide/" 2>/dev/null || {
    mkdir -p "$DEST/flowide"
    cp -R "$SRC/src/flowide/." "$DEST/flowide/"
  }
fi

# Always make a `flow` command available in ~/.local/bin without clobbering an
# existing working script from pip.
if [ ! -x "$BIN_DIR/flow" ]; then
  cat > "$BIN_DIR/flow" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$DEST\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m flowide "\$@"
EOF
  chmod +x "$BIN_DIR/flow"
fi

# ---- 4. PATH (additive, idempotent) --------------------------------------
path_line='export PATH="$HOME/.local/bin:$PATH"'
added=0
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$rc" ]; then
    if ! grep -qF '.local/bin' "$rc"; then
      printf '\n# flowide\n%s\n' "$path_line" >> "$rc"
      added=1
    fi
  fi
done
if [ ! -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.zshrc" ]; then
  if [ ! -f "$HOME/.profile" ] || ! grep -qF '.local/bin' "$HOME/.profile"; then
    printf '\n# flowide\n%s\n' "$path_line" >> "$HOME/.profile"
    added=1
  fi
fi

# ---- 5. Done -------------------------------------------------------------
say "[3/3] Done."
say "      flow is installed. Open a new shell (PATH updated) or run:"
say "      export PATH=\"\$HOME/.local/bin:\$PATH\" && flow up"
if [ "$added" -eq 1 ]; then
  say "      (added ~/.local/bin to your shell PATH)"
fi
