#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"

echo "==> Installing Mistral GTK into user environment..."

mkdir -p "$BIN_DIR"
mkdir -p "$APP_DIR"
mkdir -p "$ICON_BASE/32x32/apps"
mkdir -p "$ICON_BASE/128x128/apps"
mkdir -p "$ICON_BASE/256x256/apps"

# Link launcher script into ~/.local/bin
ln -sf "$SCRIPT_DIR/bin/mistral-gtk" "$BIN_DIR/mistral-gtk"

# Install PNG icons
if [ -f "$SCRIPT_DIR/assets/mistral-gtk-32.png" ]; then
    cp "$SCRIPT_DIR/assets/mistral-gtk-32.png" "$ICON_BASE/32x32/apps/mistral-gtk.png"
fi
if [ -f "$SCRIPT_DIR/assets/mistral-gtk-128.png" ]; then
    cp "$SCRIPT_DIR/assets/mistral-gtk-128.png" "$ICON_BASE/128x128/apps/mistral-gtk.png"
fi
if [ -f "$SCRIPT_DIR/assets/mistral-gtk-256.png" ]; then
    cp "$SCRIPT_DIR/assets/mistral-gtk-256.png" "$ICON_BASE/256x256/apps/mistral-gtk.png"
fi

# Install desktop file (single entry)
sed "s|Exec=.*|Exec=$BIN_DIR/mistral-gtk %U|g" "$SCRIPT_DIR/mistral-gtk.desktop" > "$APP_DIR/mistral-gtk.desktop"

# Update desktop and icon caches
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
fi

echo "==> Selesai! Icon dan desktop entry Mistral sudah terpasang rapi di GNOME."
echo "    Lu bisa langsung cari 'Mistral' di menu aplikasi atau ketik 'mistral-gtk' di terminal."
