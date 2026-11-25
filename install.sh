#!/bin/sh
# Installa Acquisitore VHS per l'utente corrente (nessun file di sistema toccato
# a parte i pacchetti richiesti).
set -e
DIR=$(cd "$(dirname "$0")" && pwd)

echo "== Acquisitore VHS: installazione =="

if [ "$1" != "--senza-pacchetti" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        echo "Installo i pacchetti necessari (potrebbe chiedere la password)..."
        sudo apt-get install -y python3 python3-tk ffmpeg v4l-utils alsa-utils pulseaudio-utils \
            || sudo apt-get install -y python3 python3-tk ffmpeg v4l-utils alsa-utils
        # Driver per la codifica video con la grafica Intel integrata (conversioni veloci).
        # i965 serve ai processori Celeron/Pentium più datati (es. Lenovo B50-30).
        sudo apt-get install -y i965-va-driver vainfo || true
        sudo apt-get install -y intel-media-va-driver || true
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-tkinter ffmpeg v4l-utils alsa-utils pulseaudio-utils
        sudo dnf install -y libva-intel-driver libva-utils || true
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed python tk ffmpeg v4l-utils alsa-utils libpulse
        sudo pacman -S --needed libva-intel-driver libva-utils || true
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3 python3-tk ffmpeg v4l-utils alsa-utils pulseaudio-utils
    else
        echo "Gestore pacchetti non riconosciuto: installa a mano python3, tkinter, ffmpeg, v4l-utils."
    fi
fi

mkdir -p "$HOME/.local/share/acquisitore" "$HOME/.local/bin" "$HOME/.local/share/applications"
cp "$DIR/acquisitore.py" "$HOME/.local/share/acquisitore/acquisitore.py"
chmod +x "$HOME/.local/share/acquisitore/acquisitore.py"
ln -sf "$HOME/.local/share/acquisitore/acquisitore.py" "$HOME/.local/bin/acquisitore"

cat > "$HOME/.local/share/applications/acquisitore.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Acquisitore VHS
Comment=Digitalizza le videocassette con una scheda di acquisizione USB
Exec=python3 $HOME/.local/share/acquisitore/acquisitore.py
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;Recorder;
DESK

# Permessi per usare la scheda video, la scheda audio e la grafica Intel
GRUPPI="video,audio"
if getent group render >/dev/null 2>&1; then GRUPPI="$GRUPPI,render"; fi
MANCA=0
for g in $(echo "$GRUPPI" | tr ',' ' '); do
    id -nG "$USER" | grep -qw "$g" || MANCA=1
done
if [ "$MANCA" = 1 ]; then
    echo "Aggiungo l'utente $USER ai gruppi $GRUPPI..."
    sudo usermod -aG "$GRUPPI" "$USER" && \
        echo ">> Esci e rientra nella sessione (o riavvia) perché abbia effetto."
fi

echo
echo "Fatto! Trovi 'Acquisitore VHS' nel menu Audio/Video,"
echo "oppure avvialo da terminale con:  python3 $HOME/.local/share/acquisitore/acquisitore.py"
