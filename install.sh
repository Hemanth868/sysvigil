#!/bin/sh
# Install sysvigil for the current user; no root needed.
#
#   ./install.sh                  from a checkout
#   curl -fsSL https://raw.githubusercontent.com/Hemanth868/sysvigil/main/install.sh | sh
#
# It creates a private Python environment in ~/.local/share/sysvigil, links the
# `sysvigil` command into ~/.local/bin, puts that directory on PATH if it is
# not there yet, and adds sysvigil to the app menu. Run it again to upgrade.
#
#   --uninstall        remove sysvigil (piped: ... | sh -s -- --uninstall)
#   --no-menu          do not add an app-menu entry
#   --no-modify-path   do not touch shell startup files
#
# SYSVIGIL_SOURCE overrides what is installed (a directory, archive, or URL).
set -eu

REPO=https://github.com/Hemanth868/sysvigil
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
APP_DIR=$DATA_HOME/sysvigil
VENV=$APP_DIR/venv
BIN_DIR=$HOME/.local/bin
LINK=$BIN_DIR/sysvigil
DESKTOP=$DATA_HOME/applications/sysvigil.desktop
MARKER="# Added by the sysvigil installer"

say() { printf '%s\n' "$*"; }
fail() { printf 'sysvigil install: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,15s/^# \{0,1\}//p' "$0" 2>/dev/null || say "usage: install.sh [--uninstall] [--no-menu] [--no-modify-path]"
}

action=install
menu=yes
modify_path=yes
for arg in "$@"; do
    case $arg in
        --uninstall) action=uninstall ;;
        --no-menu) menu=no ;;
        --no-modify-path) modify_path=no ;;
        -h | --help) usage; exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

# How to run this script again, for the closing hints.
if [ -f "$0" ] && [ "$(basename -- "$0")" = install.sh ]; then
    again="sh $0"
else
    again="curl -fsSL https://raw.githubusercontent.com/Hemanth868/sysvigil/main/install.sh | sh -s --"
fi

if [ "$action" = uninstall ]; then
    rm -rf "$VENV"
    rmdir "$APP_DIR" 2>/dev/null || true
    # Only remove the command if it is the link this installer made.
    if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$VENV/bin/sysvigil" ]; then
        rm -f "$LINK"
    fi
    rm -f "$DESKTOP"
    say "sysvigil is removed."
    say "A PATH line marked \"$MARKER\" in your shell startup file, if any, is left in place."
    exit 0
fi

python=
for candidate in ${PYTHON:-} python3 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        python=$candidate
        break
    fi
done
[ -n "$python" ] || fail "sysvigil needs Python 3.10 or newer (Fedora: sudo dnf install python3)"

# Install the checkout this script sits in, or else the latest from GitHub.
here=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || here=
if [ -n "${SYSVIGIL_SOURCE:-}" ]; then
    source=$SYSVIGIL_SOURCE
elif [ -n "$here" ] && grep -qs '^name = "sysvigil"' "$here/pyproject.toml"; then
    source=$here
else
    source=$REPO/archive/refs/heads/main.tar.gz
fi

say "Creating a Python environment in $VENV"
mkdir -p "$APP_DIR"
if ! "$python" -m venv --clear "$VENV"; then
    fail "could not create it; on Debian or Ubuntu, install python3-venv first"
fi
say "Installing sysvigil from $source"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "$source" ||
    fail "pip could not install sysvigil (it needs internet access to fetch psutil and textual)"

mkdir -p "$BIN_DIR"
if [ -e "$LINK" ] && ! [ -L "$LINK" ]; then
    say "Replacing the older $LINK"
fi
ln -sf "$VENV/bin/sysvigil" "$LINK"

if [ "$menu" = yes ]; then
    mkdir -p "$(dirname -- "$DESKTOP")"
    cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=sysvigil
GenericName=Resource Monitor
Comment=Live CPU, memory, GPU, sensor, disk, network, and process stats
Exec="$LINK"
Icon=utilities-system-monitor
Terminal=true
Categories=System;Monitor;
Keywords=monitor;cpu;memory;gpu;temperature;process;
EOF
fi

# Make `sysvigil` work from any directory in new terminals.
case ":$PATH:" in
    *":$BIN_DIR:"*) path_note= ;;
    *)
        if [ "$modify_path" = no ]; then
            path_note="Add $BIN_DIR to PATH to run sysvigil by name."
        else
            # Written as is: the shell expands $HOME each time it starts.
            # shellcheck disable=SC2016
            line='export PATH="$HOME/.local/bin:$PATH"'
            case ${SHELL:-} in
                */zsh) rc=$HOME/.zshrc ;;
                */bash) rc=$HOME/.bashrc ;;
                */fish)
                    rc=${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/sysvigil.fish
                    # shellcheck disable=SC2016
                    line='fish_add_path -g $HOME/.local/bin'
                    ;;
                *) rc=$HOME/.profile ;;
            esac
            if grep -qsF "$MARKER" "$rc"; then
                path_note="$BIN_DIR is on PATH in new terminals (set in $rc)."
            else
                mkdir -p "$(dirname -- "$rc")"
                printf '\n%s\n%s\n' "$MARKER" "$line" >>"$rc"
                path_note="Added $BIN_DIR to PATH in $rc; open a new terminal to use it."
            fi
        fi
        ;;
esac

"$LINK" --help >/dev/null || fail "the installed sysvigil does not run"
version=$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("sysvigil"))')
say ""
say "sysvigil $version is installed."
say "  sysvigil          open the dashboard (q quits)"
say "  sysvigil --once   print a one-second snapshot"
[ "$menu" = no ] || say "It is also in your app menu as \"sysvigil\"."
[ -z "$path_note" ] || say "$path_note"
found=$(command -v sysvigil 2>/dev/null || true)
if [ -n "$found" ] && [ "$found" != "$LINK" ]; then
    say "Note: $found comes before $LINK on PATH; remove it or run $LINK."
fi
say "Upgrade: run the installer again.  Remove: $again --uninstall"
