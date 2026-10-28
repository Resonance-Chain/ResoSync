#!/usr/bin/env bash
# Installa SaveSync sullo Steam Deck (o su qualsiasi Linux) nella tua cartella home.
# Non servono permessi di amministratore e non tocca il sistema (che su SteamOS è in sola
# lettura): sopravvive agli aggiornamenti di SteamOS.
#
# Uso, dalla cartella estratta dal file SaveSync-linux-x86_64.tar.gz:
#     bash install-steamdeck.sh              installa o aggiorna
#     bash install-steamdeck.sh --uninstall  rimuove il programma (non i tuoi dati)
set -euo pipefail

DEST="$HOME/.local/share/savesync-app"
APPS="$HOME/.local/share/applications"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now savesync.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/savesync.service"
    systemctl --user daemon-reload 2>/dev/null || true
    rm -rf "$DEST" "$APPS/savesync.desktop" "$HOME/Desktop/savesync.desktop"
    echo "SaveSync rimosso. I tuoi dati restano in $HOME/.savesync"
    exit 0
fi

if [ ! -x "$SRC/SaveSync" ]; then
    echo "Non trovo l'eseguibile SaveSync accanto a questo script."
    echo "Estrai prima SaveSync-linux-x86_64.tar.gz e lancia lo script da quella cartella."
    exit 1
fi

# se il servizio è attivo lo fermo durante l'aggiornamento
was_active=0
if systemctl --user is-active --quiet savesync.service 2>/dev/null; then
    was_active=1
    systemctl --user stop savesync.service
fi

if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$(dirname "$DEST")"
    rm -rf "$DEST.new"
    cp -a "$SRC" "$DEST.new"
    rm -rf "$DEST"
    mv "$DEST.new" "$DEST"
fi
chmod +x "$DEST/SaveSync"

mkdir -p "$APPS"
cat > "$APPS/savesync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SaveSync
Comment=Sincronizza i salvataggi dei giochi nel cloud
Exec="$DEST/SaveSync"
Icon=$DEST/icon.png
Terminal=false
Categories=Utility;Game;
EOF
if [ -d "$HOME/Desktop" ]; then
    cp "$APPS/savesync.desktop" "$HOME/Desktop/savesync.desktop"
    chmod +x "$HOME/Desktop/savesync.desktop"
fi

if [ "$was_active" = 1 ]; then
    systemctl --user start savesync.service
fi

echo
echo "SaveSync $("$DEST/SaveSync" --version 2>/dev/null | cut -d' ' -f2) installato in $DEST"
echo "Lo trovi nel menu delle applicazioni e sul desktop."
echo "Apro la finestra per configurarlo..."
nohup "$DEST/SaveSync" >/dev/null 2>&1 &
