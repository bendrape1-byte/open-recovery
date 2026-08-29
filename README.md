# open-recovery

Makes Sony camera footage playable again after recovery software hands it back broken.

## The problem this solves

You delete footage from an SD card, run a recovery tool such as 4DDiG,
and get your files back at the right size with the right names. Then nothing opens
them. Not VLC, not Premiere, not QuickTime. Every player says the file is invalid.

The video is usually still in there. Sony cameras write the picture data into one
part of the file and the index that describes it into another part at the very
end. That index holds the resolution, the frame rate, and the position of every
frame. Recovery tools often return the first part and lose the second, which
leaves you with all the pixels and no table of contents.

open-recovery rebuilds that index. Your picture is copied across untouched, so
nothing is re-encoded and no quality is lost. Everything runs on your own machine.

It was built on footage from a Sony A7 IV recording XAVC S 4K at 25p, and tested on
eleven real recovered files plus intact clips that were deliberately broken the same
way, so the result could be compared against the original frame by frame.

## What you need

- macOS or Linux, Python 3, and [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`)
- Your broken files
- For recording modes that are not built in yet, one intact clip from the same
  camera in the same mode. See *Teaching it a new mode* below.

## Using it

```bash
git clone https://github.com/bendrape1-byte/open-recovery.git
cd open-recovery
```

Put the broken files in `input/`, then:

```bash
python3 run.py
```

Repaired clips land in `recovered/`, together with `_uebersicht.png`, which holds
one still frame from each so you can see at a glance what came back.

To find out what you have without building anything, which takes seconds rather
than minutes:

```bash
python3 run.py --pruefen
```

## Reading the results

| What it says | What it means |
|---|---|
| `OK (18s Bild, 18s Ton)` | Done. 18 seconds of picture, 18 seconds of sound. |
| `LEER - nichts geborgen` | The file is 99% zeros. The recovery tool returned an empty shell and there is no video inside it. |
| `kein Referenzmodus für …` | This recording mode is not known yet. See below. |
| `PRUEFEN: …` | Something was built, but it failed the check. Do not trust it. |

`OK` is not a guess. Every finished file has to pass two tests: the container must
promise exactly as many frames as the decoder actually produces, and decoding the
whole clip must raise zero errors. A file with its frames in the wrong order fails
that check, which is the point. Someone watching the first few seconds would not
have caught it.

## The one thing you have to decide

Frames are always stored sideways in the file. The only record that the camera was
held upright lived in the index, and the index is what got lost, so no software can
work out which way is up.

Open `recovered/_uebersicht.png`. Any clip lying on its side gets rebuilt with:

```bash
python3 run.py --drehen 90 C0056 C0057
```

If it comes out upside down instead, use `--drehen 270`.

## Teaching it a new mode

Four modes ship with it: 4K 25p in both 10-bit 4:2:2 and 8-bit 4:2:0, and 1080p in
50p and 60p. Anything else, such as 4K 50p or 1080p 25p, gets reported honestly as
unknown instead of turned into garbage.

To add one you need a healthy clip shot on the same camera in exactly that mode:
same resolution, same frame rate, same bit depth. A few seconds is plenty, and
filming a new one works fine.

```bash
python3 run.py --referenz /path/to/a/healthy/clip.MP4
```

Only the codec settings get extracted, a few hundred bytes. The clip itself is not
needed afterwards and can be deleted.

## What does not come back

Recording date, timecode and camera model were stored in the lost index. They also
sit in the `C####M01.XML` files the camera writes next to every clip, so if your
recovery tool returned those, the information still exists.

A few frames go missing at the start of each clip, the ones that referenced a
keyframe that did not survive, and sometimes one at the end where the recovery cut
mid-frame. In practice that is two to thirteen frames out of several hundred.

## Limits

- H.264 only, which covers XAVC S and XAVC S-I. XAVC HS is HEVC and will not work.
  Those files show up as `kein Video` rather than `LEER`.
- Memory use is roughly one and a half times the largest single file.
- Speed is about 11 seconds per gigabyte.

## How it works

Three things have to be rebuilt, and each one is recovered from the picture data
itself rather than guessed.

The codec settings come first. Resolution, bit depth and quantisation tables live
in the lost index and never in the picture data, so they get grafted from a
reference clip shot in the same mode. Picking the right reference is automatic: the
resolution is measured from the frames, the frame rate is derived from how much
audio sits between video chunks, and then a few frames are test-decoded against
each candidate. A reference with the wrong bit depth decodes into noise, and the
decoder says so.

The sound is easier. Uncompressed audio sits at the head of each gap between video
chunks, ending where the camera's metadata track begins.

The frame order is the subtle one. Compressed video is not stored in the order you
watch it, because frames that depend on later frames have to be decoded first and
displayed afterwards. The table that untangles this was in the lost index too. It
gets recomputed by reading each frame's picture order count straight out of the
bitstream.

---

`LIESMICH.md` is the same guide in German. MIT licensed.
Written with [Claude Code](https://claude.com/claude-code).
