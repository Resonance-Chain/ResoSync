#!/usr/bin/env bash
# Installa ResoSync sullo Steam Deck (o su qualsiasi Linux) nella tua cartella home.
# Non servono permessi di amministratore e non tocca il sistema (che su SteamOS è in sola
# lettura): sopravvive agli aggiornamenti di SteamOS.
#
# Uso, dalla cartella estratta dal file ResoSync-linux-x86_64.tar.gz:
#     bash install-steamdeck.sh              installa o aggiorna
#     bash install-steamdeck.sh --uninstall  rimuove il programma (non i tuoi dati)
set -euo pipefail

DEST="$HOME/.local/share/resosync-app"
LEGACY="$HOME/.local/share/savesync-app"  # nome del progetto fino alla 0.4.0
APPS="$HOME/.local/share/applications"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now resosync.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/resosync.service"
    systemctl --user daemon-reload 2>/dev/null || true
    rm -rf "$DEST" "$APPS/resosync.desktop" "$HOME/Desktop/resosync.desktop"
    [ -L "$LEGACY" ] && rm -f "$LEGACY"
    echo "ResoSync rimosso. I tuoi dati restano in $HOME/.resosync"
    exit 0
fi

if [ ! -x "$SRC/ResoSync" ]; then
    echo "Non trovo l'eseguibile ResoSync accanto a questo script."
    echo "Estrai prima ResoSync-linux-x86_64.tar.gz e lancia lo script da quella cartella."
    exit 1
fi

# se il servizio è attivo lo fermo durante l'aggiornamento
was_active=0
if systemctl --user is-active --quiet resosync.service 2>/dev/null; then
    was_active=1
    systemctl --user stop resosync.service
fi

# Aggiornamento da SaveSync: tolgo la vecchia cartella e i vecchi collegamenti. Il vecchio
# servizio lo converte ResoSync al primo avvio (lo script lo apre alla fine).
from_savesync=0
if [ -d "$LEGACY" ] && [ ! -L "$LEGACY" ]; then
    from_savesync=1
    systemctl --user stop savesync.service 2>/dev/null || true
    rm -rf "$LEGACY" "$APPS/savesync.desktop" "$HOME/Desktop/savesync.desktop"
fi

if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$(dirname "$DEST")"
    rm -rf "$DEST.new"
    cp -a "$SRC" "$DEST.new"
    rm -rf "$DEST"
    mv "$DEST.new" "$DEST"
fi
chmod +x "$DEST/ResoSync"

if [ "$from_savesync" = 1 ]; then
    # le opzioni di avvio di Steam già impostate puntano a .../savesync-app/SaveSync
    ln -sfn "$DEST" "$LEGACY"
    ln -sf ResoSync "$DEST/SaveSync"
fi

mkdir -p "$APPS"
cat > "$APPS/resosync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ResoSync
Comment=Sincronizza i salvataggi dei giochi nel cloud
Exec="$DEST/ResoSync"
Icon=$DEST/icon.png
Terminal=false
Categories=Utility;Game;
EOF
if [ -d "$HOME/Desktop" ]; then
    cp "$APPS/resosync.desktop" "$HOME/Desktop/resosync.desktop"
    chmod +x "$HOME/Desktop/resosync.desktop"
fi

if [ "$was_active" = 1 ]; then
    systemctl --user start resosync.service
fi

echo
echo "ResoSync $("$DEST/ResoSync" --version 2>/dev/null | cut -d' ' -f2) installato in $DEST"
echo "Lo trovi nel menu delle applicazioni e sul desktop."
echo "Apro la finestra per configurarlo..."
nohup "$DEST/ResoSync" >/dev/null 2>&1 &
