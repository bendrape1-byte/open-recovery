#!/usr/bin/env python3
"""Sony XAVC recovery - everything in input/ becomes a playable file in recovered/.

  python3 run.py                      alles aus input/ wiederherstellen
  python3 run.py --pruefen            nur pruefen, nichts bauen (schnell)
  python3 run.py --drehen 90 C0056    einzelne Clips hochkant neu bauen
  python3 run.py --referenz DATEI.MP4 einen neuen Aufnahmemodus anlernen
  python3 run.py --ordner /Volumes/SSD/recovery   anderer Arbeitsordner

Bildrate, Aufloesung und der passende Referenz-Parametersatz werden je Datei aus
dem Material selbst bestimmt. Die Ausrichtung nicht: die Frames liegen immer quer
im Stream, nur die verlorene tkhd-Matrix wusste, dass die Kamera hochkant stand.
Dafuer legt der Lauf recovered/_uebersicht.png an - ein Blick genuegt.
"""
import base64, json, os, re, shutil, statistics, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import recover_xavc as rx

IN, OUT = os.path.join(HERE, "input"), os.path.join(HERE, "recovered")
DONORS = os.path.join(HERE, "lib", "donors.json")
SHEET = "_uebersicht.png"
SWITCHES = ("--pruefen",)                  # Schalter ohne Wert
RATES = (24, 25, 30, 50, 60)
CHROMA = {100: "8-bit 4:2:0", 110: "10-bit 4:2:0", 122: "10-bit 4:2:2", 244: "4:4:4"}
# Cameras preallocate mdat in fixed blocks, so a good clip can be mostly zero
# padding. Judge on how much video is there, not on its share of the file.
MIN_VIDEO = 2_000_000
# A carved region often holds photos rather than video: the recovery tool takes the
# name from a directory entry that does not match what is actually in the clusters.
# Sony raw files carry a full preview near their front, which frequently survives
# even when the sensor data behind it does not.
JPEG_START = re.compile(b'\xff\xd8\xff[\xe0-\xef]')
PHOTO_MIN = 50_000                 # below that it is the thumbnail, not the preview

# ------------------------------------------------------------ reference clips

def load_donors():
    """Parameter sets only - the multi-gigabyte reference clips are not needed."""
    if not os.path.exists(DONORS): return []
    out = []
    for x in json.load(open(DONORS))["donors"]:
        out.append({**x,
                    "sps": [base64.b64decode(s) for s in x["sps"]],
                    "pps": [base64.b64decode(p) for p in x["pps"]]})
    return out

def add_donor(path):
    sps, pps = rx.parameter_sets(path)
    w, h = rx.sps_geometry(sps[0])
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
                        "stream=r_frame_rate","-of","csv=p=0", path],
                       capture_output=True, text=True).stdout.strip().strip(',')
    num, den = (r.split("/") + ["1"])[:2]
    fps = min(RATES, key=lambda x: abs(x - int(num)/int(den)))
    entry = {"name": f"{w*16}x{h*16} {fps}p {CHROMA.get(sps[0][1], f'profile {sps[0][1]}')}",
             "from": os.path.basename(path), "mbs": w*h, "fps": fps,
             "profile": sps[0][1], "level": sps[0][3]/10,
             "pps_ids": sorted(rx.Bits(rx.rbsp(x[1:])).ue() for x in pps),
             "sps": [base64.b64encode(x).decode() for x in sps],
             "pps": [base64.b64encode(x).decode() for x in pps]}
    doc = json.load(open(DONORS)) if os.path.exists(DONORS) else {"donors": []}
    doc["donors"] = [d for d in doc["donors"] if d["name"] != entry["name"]] + [entry]
    json.dump(doc, open(DONORS, "w"), indent=2)
    print(f"angelernt: {entry['name']}  (PPS {entry['pps_ids']}, aus {entry['from']})")

# ------------------------------------------------------------ per file

def analyse(d, segs):
    """Macroblocks, needed PPS ids and frame rate, straight from the payload.

    The rate falls out of the interleave: every gap holds the LPCM for the chunk
    before it, so audio seconds against frames in that chunk give it.
    """
    max_fmb, ids, per_chunk = 0, set(), []
    for a, _ in segs:
        n0 = 0
        for n in rx.chunk_nals(d, a):
            if n[0] & 0x1f not in (1, 5): continue
            r = rx.Bits(rx.rbsp(n[1:48]))
            fmb = r.ue(); r.ue(); ids.add(r.ue())
            max_fmb = max(max_fmb, fmb)
            if fmb == 0: n0 += 1
        per_chunk.append(n0)
    audio = []
    for i in range(len(segs) - 1):
        k = rx.klv_at(d, segs[i][1], segs[i+1][0])
        if k > rx.AUDIO_HDR: audio.append(k - rx.AUDIO_HDR)
    if not audio or len(per_chunk) < 2:
        return max_fmb, ids, None, None
    raw = statistics.median(per_chunk[:len(audio)]) / (statistics.median(audio) / rx.AUDIO_RATE)
    return max_fmb, ids, min(RATES, key=lambda r: abs(r - raw)), raw

def probe_chunk(d, segs):
    """The richest chunk that starts on an IDR, or None.

    A badly fragmented file leaves chunks holding a piece of a frame. Those cannot
    decode cleanly no matter which reference is used, so testing on one rejects
    every candidate. Pick the chunk with the most material behind its IDR instead.
    """
    best = None
    for a, _ in segs:
        ns = rx.chunk_nals(d, a)
        idr = next((i for i, n in enumerate(ns) if n[0] & 0x1f == 5), None)
        if idr is None: continue
        while idr > 0 and ns[idr-1][0] & 0x1f in (6, 9): idr -= 1
        if best is None or len(ns) - idr > len(best[0]) - best[1]:
            best = (ns, idr)
    return best

def probe(chunk, donor):
    """Decode a few frames. A reference with the wrong bit depth or chroma format
    parses the slice data into noise, and the decoder says so."""
    if chunk is None: return 10**6
    ns, idr = chunk
    with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as f:
        f.write(b''.join(b'\x00\x00\x00\x01' + x for x in donor["sps"] + donor["pps"]))
        for n in ns[idr:]: f.write(b'\x00\x00\x00\x01' + n)
        tmp = f.name
    r = subprocess.run(["ffmpeg","-v","error","-f","h264","-i",tmp,
                        "-frames:v","8","-f","null","-"], capture_output=True, text=True)
    os.unlink(tmp)
    return sum(1 for l in r.stderr.splitlines() if "error" in l or "corrupt" in l)

def find_photos(d):
    """Byte ranges of the embedded JPEGs, largest variant only."""
    out = []
    for m in JPEG_START.finditer(d):
        s = m.start()
        e = d.find(b'\xff\xd9', s)
        if e > 0 and e - s >= PHOTO_MIN: out.append((s, e + 2))
    return out

def extract_photos(d, name):
    """Write the photos out and keep only the ones that actually decode."""
    outdir = os.path.join(OUT, os.path.splitext(name)[0] + "_fotos")
    os.makedirs(outdir, exist_ok=True)
    kept = []
    for i, (a, b) in enumerate(find_photos(d), 1):
        p = os.path.join(outdir, f"{i:03d}.jpg")
        open(p, 'wb').write(d[a:b])
        ok = subprocess.run(["ffprobe","-v","error","-show_entries","stream=width",
                             "-of","csv=p=0", p], capture_output=True, text=True)
        if ok.returncode == 0 and ok.stdout.strip(): kept.append(p)
        else: os.unlink(p)
    if kept: sheet(kept, os.path.join(outdir, SHEET), seek=None)
    else: os.rmdir(outdir)
    return kept, outdir

def check(path):
    """What the container promises against what the decoder actually hands out."""
    def run(c): return subprocess.run(c, capture_output=True, text=True)
    n = run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
             "packet=pts","-of","csv=p=0", path]).stdout
    m = run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
             "frame=best_effort_timestamp","-of","csv=p=0", path]).stdout
    e = run(["ffmpeg","-v","error","-i", path, "-f","null","-"]).stderr
    return (len([l for l in n.splitlines() if l.strip()]),
            len([l for l in m.splitlines() if l.strip()]),
            sum(1 for l in e.splitlines() if "error" in l or "corrupt" in l))

def handle(name, donors, rot, triage_only):
    path = os.path.join(IN, name)
    size = f"{os.path.getsize(path)/1e6:.0f} MB"
    d = open(path, 'rb').read()
    if not d:
        return (name, size, "-", "-", "-", "LEER - 0 Byte")
    data = (len(d) - d.count(0)) / len(d)
    segs = rx.segments(d)
    vbytes = sum(b - a for a, b in segs)
    if vbytes < MIN_VIDEO:
        photos = find_photos(d)
        if photos and triage_only:
            return (name, size, "-", "Fotos", str(len(photos)),
                    f"{len(photos)} Fotos gefunden, kein Video")
        if photos:
            kept, outdir = extract_photos(d, name)
            return (name, size, "-", "Fotos", str(len(kept)),
                    f"{len(kept)} Fotos gesichert in {os.path.basename(outdir)}/")
        return (name, size, "-", "-", "-",
                "LEER - nichts geborgen" if data < 0.05
                else f"kein Video, keine Fotos ({data*100:.0f}% Daten)")

    mbs, ids, fps, raw = analyse(d, segs)
    if not fps:
        return (name, size, "-", "-", "-", "Bildrate nicht messbar")
    # Geometry and PPS coverage are not enough: a reference from the same
    # resolution but another frame rate decodes without complaint and still lands
    # a few dB off, because the rest of its SPS/PPS was tuned for that mode.
    fits = [c for c in donors if c["mbs"] > mbs and c["fps"] == fps
            and set(ids) <= set(c["pps_ids"])]
    if not fits:
        return (name, size, f"{fps}p", "-", "-",
                f"kein Referenzmodus: {mbs+1}+ Makrobloecke, {fps}p, PPS {sorted(ids)}")
    chunk = probe_chunk(d, segs)
    pick = next((c for c in fits if probe(chunk, c) == 0), None)
    if not pick:
        return (name, size, f"{fps}p", "-", "-",
                f"{len(fits)} Referenzmodus/e passen formal, keiner besteht die "
                f"Probedekodierung")
    if triage_only:
        return (name, size, f"{fps}p", pick["name"], "-", f"bereit ({vbytes/1e6:.0f} MB Video)")

    out = os.path.join(OUT, os.path.splitext(name)[0] + "_recovered.mov")
    cover = vbytes / len(d)
    del d, segs
    try:
        frames, secs, _ = rx.rebuild(path, pick["sps"], pick["pps"], out, fps, rot,
                                     log=lambda *_: None)
    except SystemExit as e:
        return (name, size, f"{fps}p", pick["name"], "-", str(e))
    boxed, shown, errs = check(out)
    drift = "" if abs(raw - fps) / fps < 0.01 else f", gemessen {raw:.2f}"
    # far less picture than the file could hold means the carve returned it in
    # pieces, and then the surviving sound no longer lines up with what is left
    # only a fraction of the file held a usable chain, so the carve returned this
    # clip in pieces and most of it is simply not there
    if cover < 0.5:
        drift += f", nur {cover*100:.0f}% der Datei war brauchbar"
    return (name, size, f"{fps}p", pick["name"], str(shown),
            f"OK ({frames/fps:.0f}s Bild, {secs:.0f}s Ton){drift}" if boxed == shown and errs == 0
            else f"PRUEFEN: {boxed}/{shown} Frames, {errs} Fehler")

# ------------------------------------------------------------ overview image

def sheet(paths, out, seek="1.5", cols=None):
    """One still per file, tiled. The fastest way to see what came back."""
    if not paths: return None
    cols = cols or min(3 if seek else 7, len(paths))
    rows = (len(paths) + cols - 1) // cols
    side = ["400","300"] if seek else ["260","174"]
    with tempfile.TemporaryDirectory() as tmp:
        for i, p in enumerate(paths):
            label = os.path.splitext(os.path.basename(p))[0].split("_")[0]
            cmd = ["ffmpeg","-v","error"] + (["-ss", seek] if seek else []) + ["-i", p,
                "-vf", f"scale={side[0]}:{side[1]}:force_original_aspect_ratio=decrease,"
                       f"pad={side[0]}:{side[1]}:(ow-iw)/2:(oh-ih)/2:gray,"
                       f"drawtext=text='{label}':x=5:y=4:fontsize=20:fontcolor=yellow:"
                       f"box=1:boxcolor=black@0.7",
                "-frames:v","1","-y", os.path.join(tmp, f"{i:03d}.png")]
            subprocess.run(cmd, capture_output=True)
        subprocess.run(["ffmpeg","-v","error","-framerate","1","-pattern_type","glob",
                        "-i", os.path.join(tmp, "*.png"),
                        "-vf", f"tile={cols}x{rows}", "-frames:v","1","-y", out],
                       capture_output=True)
    return out if os.path.exists(out) else None

# ------------------------------------------------------------ main

def preflight():
    """Say what is missing instead of dying inside a subprocess call later on."""
    if sys.version_info < (3, 8):
        sys.exit(f"Python 3.8 or newer required, found {sys.version.split()[0]}.")
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        sys.exit(f"{' and '.join(missing)} not found on your PATH. Install it:\n"
                 "  macOS          brew install ffmpeg\n"
                 "  Debian/Ubuntu  sudo apt install ffmpeg\n"
                 "  Windows        winget install Gyan.FFmpeg\n"
                 "Then open a new terminal and try again.")

def parse(argv):
    """--flag wert Paare einsammeln, alles Uebrige ist ein Dateinamensfilter."""
    flags, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if not a.startswith("--"): rest.append(a); i += 1
        elif a in SWITCHES:        flags[a] = True; i += 1
        else:                      flags[a] = argv[i+1] if i+1 < len(argv) else ""; i += 2
    return flags, rest

def space_check(need):
    """Output plus the elementary stream it is built from both live on the target,
    so a run wants about twice the material it reads."""
    free = shutil.disk_usage(OUT).free
    if free < need * 2:
        print(f"  Achtung: {free/1e9:.1f} GB frei, gebraucht werden etwa "
              f"{need*2/1e9:.1f} GB. Der Lauf kann mittendrin abbrechen.\n")

def main():
    preflight()
    flags, only = parse(sys.argv[1:])
    if "--referenz" in flags:
        return add_donor(flags["--referenz"])
    triage = "--pruefen" in flags
    rot = int(flags.get("--drehen") or 0)

    global IN, OUT
    if flags.get("--ordner"):
        base = os.path.abspath(os.path.expanduser(flags["--ordner"]))
        if not os.path.isdir(base):
            sys.exit(f"Ordner gibt es nicht: {base}")
        IN, OUT = os.path.join(base, "input"), os.path.join(base, "recovered")
    os.makedirs(IN, exist_ok=True); os.makedirs(OUT, exist_ok=True)
    if not os.access(OUT, os.W_OK):
        sys.exit(f"Kein Schreibrecht in {OUT}")
    donors = load_donors()
    if not donors:
        sys.exit("Keine Referenzmodi hinterlegt. Einen anlernen:\n"
                 "  python3 run.py --referenz /pfad/zu/einem/heilen/clip.MP4")

    # macOS drops a "._name" companion next to every file it copies onto exFAT,
    # holding extended attributes and no video. They sort to the top of the list
    # and would fill the report with empty rows.
    names = sorted(f for f in os.listdir(IN)
                   if f.lower().endswith((".mp4", ".mov")) and not f.startswith("."))
    if only:
        names = [n for n in names if any(o in n for o in only)]
    if not names:
        sys.exit(f"Nichts zu tun - lege die Dateien in {IN}/ ab.")

    print(f"  aus  {IN}\n  nach {OUT}\n")
    if not triage:
        space_check(sum(os.path.getsize(os.path.join(IN, n)) for n in names))
    print(f"{len(names)} Datei(en), {len(donors)} Referenzmodi"
          f"{', nur pruefen' if triage else ''}{f', {rot} Grad gedreht' if rot else ''}\n")
    rows = []
    for name in names:
        out = os.path.join(OUT, os.path.splitext(name)[0] + "_recovered.mov")
        if not triage and not only and os.path.exists(out):
            print(f"  {name:<22} schon da - uebersprungen")
            continue
        try:
            row = handle(name, donors, rot, triage)
        except Exception as e:                     # one bad file must not stop the run
            row = (name, f"{os.path.getsize(os.path.join(IN, name))/1e6:.0f} MB",
                   "-", "-", "-", f"FEHLER: {type(e).__name__}: {e}")
        rows.append(row)
        print(f"  {row[0]:<22} {row[5]}")
    if not rows: return

    hdr = ("Datei", "Groesse", "Rate", "Modus", "Frames", "Befund")
    w = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(6)]
    print()
    print("  " + "  ".join(h.ljust(w[i]) for i, h in enumerate(hdr)))
    print("  " + "  ".join("-" * w[i] for i in range(6)))
    for r in rows:
        print("  " + "  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))

    if triage: return
    movs = sorted(os.path.join(OUT, f) for f in os.listdir(OUT) if f.endswith(".mov"))
    sheet_path = sheet(movs, os.path.join(OUT, SHEET))
    if sheet_path:
        rel = os.path.relpath(sheet_path, HERE)
        print(f"\n  Uebersicht: {sheet_path if rel.startswith('..') else rel}")
        if rot == 0:
            print("  Liegt darin ein Clip auf der Seite, ihn hochkant neu bauen:")
            print("    python3 run.py --drehen 90 C0056 C0057")

if __name__ == "__main__":
    main()
