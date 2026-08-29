# Sony XAVC wiederherstellen

Baut aus geborgenen Sony-Clips, denen der `moov`-Atom fehlt, wieder abspielbare
Dateien. Betrifft alles, was 4DDiG & Co. als „wiederhergestellt" ausspucken und
was danach kein Player öffnet.

## Der eine Befehl

Dateien nach `input/` legen, dann:

```bash
cd ~/claude-code/file_recovery && python3 run.py
```

Ergebnisse landen in `recovered/`, dazu `recovered/_uebersicht.png` mit je einem
Standbild pro Clip. Fertige Dateien werden bei einem zweiten Lauf übersprungen.

## Varianten

```bash
python3 run.py --pruefen              # nur prüfen, nichts bauen (Sekunden statt Minuten)
python3 run.py --drehen 90 C0056 C0057   # einzelne Clips hochkant neu bauen
python3 run.py --referenz KLIP.MP4    # einen neuen Aufnahmemodus anlernen
```

## Die Tabelle lesen

| Befund | heißt |
|---|---|
| `OK (18s Bild, 18s Ton)` | fertig und geprüft |
| `LEER - nichts geborgen` | die Datei ist zu 99 % Nullen, da ist kein Video drin |
| `kein Referenzmodus für …` | Aufnahmemodus unbekannt → `--referenz` (siehe unten) |
| `PRUEFEN: …` | gebaut, aber die Prüfung schlug an — sag mir Bescheid |

`OK` bedeutet: der Container verspricht genau so viele Frames, wie der Decoder
tatsächlich ausgibt, und dabei fällt kein einziger Fehler an. Das ist der Test,
der Reihenfolgefehler und fehlende Bildteile findet — „spielt ab" tut es auch
mit vertauschten Frames.

## Ausrichtung — das musst du selbst entscheiden

Die Frames liegen im Stream **immer quer**. Dass die Kamera hochkant stand, wusste
nur die Display-Matrix im `tkhd`, und die lag im verlorenen `moov`. Aus den Daten
ist das nicht ableitbar.

Deshalb: einmal normal laufen lassen, `recovered/_uebersicht.png` anschauen, und
die Clips, die auf der Seite liegen, mit `--drehen 90` neu bauen. Falls sie danach
auf dem Kopf stehen, `--drehen 270`.

## Neuer Aufnahmemodus

Hinterlegt sind 4K 25p (10-bit 4:2:2 und 8-bit 4:2:0) sowie 1080p in 50p und 60p.
Für alles andere — 4K 50p, 1080p 25p — meldet der Lauf „kein Referenzmodus" statt
Müll zu bauen. Dann brauchst du **einen heilen Clip aus genau diesem Modus**:
gleiche Kamera, gleiche Auflösung, gleiche Bildrate, gleiche Bittiefe. Ein paar
Sekunden reichen, notfalls neu gedreht.

```bash
python3 run.py --referenz /pfad/zum/heilen/clip.MP4
```

Daraus werden nur die Parametersätze gezogen (ein paar hundert Byte, in
`lib/donors.json`). Der Clip selbst wird danach nicht mehr gebraucht und darf weg.

## Was nicht zurückkommt

Aufnahmedatum, Timecode und Kameramodell standen im `moov` und sind weg. Sie
stehen aber in den `C####M01.XML`-Dateien, die die Kamera neben jeden Clip legt —
falls die mitgeborgen wurden, lassen sie sich zurückschreiben.

## Grenzen

- Nur H.264 (XAVC S / XAVC S-I). **XAVC HS ist HEVC** und läuft hier nicht durch;
  solche Dateien erscheinen als „kein Video", nicht als „LEER".
- Der Arbeitsspeicher braucht etwa das Anderthalbfache der größten Einzeldatei.
  Bei 24 GB RAM sind 4-GB-Clips kein Problem, solange nichts parallel läuft.
- Rechenzeit: rund 11 Sekunden pro Gigabyte.

## Was wo liegt

```
input/            hier rein
recovered/        hier raus, plus _uebersicht.png
run.py            der Befehl
lib/recover_xavc.py   die eigentliche Arbeit, auch einzeln aufrufbar
lib/donors.json       die hinterlegten Aufnahmemodi
lib/verify.py         zeigt elst/stts/ctts einer fertigen Datei
```

Einzelne Datei ohne den Stapellauf:

```bash
python3 lib/recover_xavc.py KAPUTT.MP4 REFERENZ.MP4 AUSGABE.mov 25 90
```

---

Copyright (c) 2026 Ben Drape. Alle Rechte vorbehalten.
