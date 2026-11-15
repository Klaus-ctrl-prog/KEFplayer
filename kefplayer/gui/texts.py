"""Alle deutschen Texte der Oberflaeche an einer Stelle (keine Zahlen oder Aussagen ueber die KEF, die nicht belegt sind)."""

NEUTRAL_SUMMARY = "Profil: neutral. Dateien werden unveraendert gesendet."
NEUTRAL_CONVERT_SUMMARY = "Profil: neutral. Dateien werden umgewandelt ({what})."
PROFILE_SUMMARY = "Profil: {name}. Dateien werden verarbeitet (Preamp {preamp})."

VOLUME_HINT = "Die Lautstaerke der KEF wird nicht veraendert."
SIGNALPATH_HINT = (
    "Das Profil wirkt nur auf Wiedergabe ueber KEFplayer (PC -> DLNA). Equalizer APO, TV (optisch), "
    "Bluetooth und andere KEF-Quellen bleiben unveraendert. Kanalunterschiede gibt es nur hier."
)
FIREWALL_HINT = (
    "Der Titel startete nie und der Renderer hat keine Datei abgerufen. Moegliche Ursache: Windows-Firewall "
    "blockiert den Medienserver. Zugriff nur im privaten Netz erlauben (Standard-Port TCP 8765)."
)
NO_RENDERER_HINT = (
    "Kein Renderer gefunden. Tipps: gleiches Netz pruefen, bei mehreren Netzwerkkarten die eigene IP "
    "(Bind-IP) setzen, die Adresse der Geraetebeschreibung (http://...) direkt eintragen, Firewall (UDP 1900) pruefen."
)
REJECTED_HINT = (
    "Der Renderer hat den Titel nicht gespielt. Ausweichoptionen (unverifiziert): MIME erzwingen "
    "(z. B. audio/x-flac) oder einfache protocolInfo verwenden."
)

ERR_NO_DEVICE = "Kein Geraet gewaehlt. Zuerst ein Geraet suchen und auswaehlen."
ERR_EMPTY_QUEUE = "Die Warteschlange enthaelt keine abspielbaren Dateien."
ERR_BUSY = "Es laeuft bereits ein Vorgang ({state})."
ERR_SERVER = "Medienserver konnte nicht starten ({error}). Anderen Port waehlen."
ERR_NOT_AUDIO = "Keine unterstuetzte Audiodatei: {name}"
ERR_UNREADABLE = "Nicht lesbar: {name} ({error})"
