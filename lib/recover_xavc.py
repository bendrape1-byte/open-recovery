#!/usr/bin/env python3
"""Rebuild a playable file from a carved Sony XAVC clip that lost its moov atom.

The carved file is pure mdat payload: length-prefixed H.264 NAL units, with the
audio and KLV metadata chunks interleaved between the video chunks. Three things
lived in the missing moov and have to be reconstructed:

  1. SPS/PPS - grafted from the avcC box of a reference clip shot in the SAME
     camera mode (same resolution, bit depth, chroma subsampling).
  2. The audio - LPCM 16-bit big-endian at the head of every gap chunk, cut where
     the SMPTE label 06 0E 2B 34 starts the metadata track.
  3. ctts - the composition offsets. Without them a player shows the frames in
     decode order, so B-frames land in the wrong place. They are recomputed from
     each slice's picture order count and patched into the muxed file.

  python3 recover_xavc.py BROKEN.MP4 REFERENCE.MP4 OUT.mov [fps] [rotation]
"""
import collections, os, re, struct, subprocess, sys

# NAL types a Sony XAVC video chunk actually contains, with a plausible size cap.
# Without the cap a mis-read length prefix swallows megabytes of audio as a single
# "NAL", and the start codes inside it then confuse every downstream parser.
NAL_LIMITS = {1: 4_000_000,   # slice
              5: 4_000_000,   # IDR slice
              6: 1 << 16,     # SEI
              7: 1024,        # SPS
              8: 1024,        # PPS
              9: 8}           # access unit delimiter
KLV        = b'\x06\x0e\x2b\x34'   # SMPTE universal label = start of metadata track
AUDIO_HDR  = 28                    # chunk header sitting just before the KLV packet
AUDIO_RATE = 48000 * 2 * 2         # LPCM 16 bit, stereo

# A correctly escaped NAL can never contain 00 00 00/01/02 - emulation prevention
# exists to make that impossible. Finding one means the length chain slipped and
# read audio as a slice; those fakes carry start codes that split into bogus
# access units downstream, so they get dropped.
NOT_A_NAL = re.compile(b'\x00\x00[\x00\x01\x02]')

# Every video chunk opens with an access unit delimiter, and an AUD is 2 bytes,
# so its AVCC record always reads 00 00 00 02 09. That makes chunk starts findable
# with a plain byte search instead of trying every offset - which matters, because
# the gap to the next chunk is sometimes tens of megabytes.
CHUNK_START = b'\x00\x00\x00\x02\x09' 

# ---------------------------------------------------------------- bitstream

def rbsp(b):
    """Strip emulation prevention bytes."""
    o, i = bytearray(), 0
    while i < len(b):
        if i+2 < len(b) and b[i] == 0 and b[i+1] == 0 and b[i+2] == 3:
            o += b[i:i+2]; i += 3
        else:
            o.append(b[i]); i += 1
    return bytes(o)

class Bits:
    def __init__(s, b): s.b, s.p = b, 0
    def u1(s):
        v = (s.b[s.p>>3] >> (7-(s.p&7))) & 1; s.p += 1; return v
    def u(s, n):
        v = 0
        for _ in range(n): v = (v<<1) | s.u1()
        return v
    def ue(s):
        z = 0
        while s.u1() == 0: z += 1
        return (1<<z)-1 + s.u(z) if z else 0
    def se(s):
        k = s.ue(); return (k+1)//2 if k % 2 else -(k//2)

def parse_sps(sps):
    """-> (log2_max_frame_num, pic_order_cnt_type, log2_max_poc_lsb)"""
    r = Bits(rbsp(sps[1:])); prof = r.u(8); r.u(8); r.u(8); r.ue()
    if prof in (100,110,122,244,44,83,86,118,128,138,139,134,135):
        chroma = r.ue()
        if chroma == 3: r.u1()
        r.ue(); r.ue(); r.u1()
        if r.u1():                                    # scaling matrix
            for i in range(8 if chroma != 3 else 12):
                if r.u1():
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt: nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    log2_fn = r.ue() + 4
    poc_type = r.ue()
    log2_poc = 0
    if poc_type == 0:
        log2_poc = r.ue() + 4
    elif poc_type == 1:
        r.u1(); r.se(); r.se()
        for _ in range(r.ue()): r.se()
    return log2_fn, poc_type, log2_poc

def sps_geometry(sps):
    """-> (macroblocks wide, macroblocks tall). Identifies the recording mode."""
    r = Bits(rbsp(sps[1:])); prof = r.u(8); r.u(8); r.u(8); r.ue()
    if prof in (100,110,122,244,44,83,86,118,128,138,139,134,135):
        chroma = r.ue()
        if chroma == 3: r.u1()
        r.ue(); r.ue(); r.u1()
        if r.u1():
            for i in range(8 if chroma != 3 else 12):
                if r.u1():
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt: nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    r.ue()                                            # log2_max_frame_num_minus4
    poc_type = r.ue()
    if poc_type == 0: r.ue()
    elif poc_type == 1:
        r.u1(); r.se(); r.se()
        for _ in range(r.ue()): r.se()
    r.ue(); r.u1()                                    # max_num_ref_frames, gaps_allowed
    w = r.ue() + 1
    h = r.ue() + 1
    if not r.u1(): h *= 2                             # frame_mbs_only_flag
    return w, h

def access_units(nals, log2_fn, poc_type, log2_poc):
    """One entry per frame: (nal_index, is_idr, poc). Only the first slice counts."""
    if poc_type != 0:
        sys.exit(f"pic_order_cnt_type {poc_type} not handled")
    MAX = 1 << log2_poc
    prev_msb = prev_lsb = 0
    out = []
    for i, n in enumerate(nals):
        t, ref = n[0] & 0x1f, (n[0] >> 5) & 3
        if t not in (1, 5): continue
        r = Bits(rbsp(n[1:48]))                       # the header fits in a few bytes
        if r.ue() != 0: continue                      # not the start of a frame
        r.ue(); r.ue(); r.u(log2_fn)                  # slice_type, pps_id, frame_num
        if t == 5: r.ue()                             # idr_pic_id
        lsb = r.u(log2_poc)
        if t == 5:
            msb = prev_msb = prev_lsb = 0
        elif lsb < prev_lsb and prev_lsb - lsb >= MAX//2: msb = prev_msb + MAX
        elif lsb > prev_lsb and lsb - prev_lsb >  MAX//2: msb = prev_msb - MAX
        else: msb = prev_msb
        poc = msb + lsb
        if ref: prev_msb, prev_lsb = msb, lsb
        out.append((i, t == 5, poc))
    return out

def composition_offsets(au):
    """Display order is only defined inside an IDR period - POC resets at each IDR."""
    idrs = [k for k, (_, isidr, _) in enumerate(au) if isidr] or [0]
    bounds = idrs + [len(au)]
    disp, base = [0]*len(au), 0
    for a, b in zip(bounds, bounds[1:]):
        for rank, k in enumerate(sorted(range(a, b), key=lambda k: au[k][2])):
            disp[k] = base + rank
        base += b - a
    return [disp[k] - k for k in range(len(au))]

# ---------------------------------------------------------------- mp4 boxes

def boxes(d, start, end):
    p = start
    while p + 8 <= end:
        sz = struct.unpack(">I", d[p:p+4])[0]; typ = d[p+4:p+8]
        if sz == 1: sz = struct.unpack(">Q", d[p+8:p+16])[0]
        if sz < 8: return
        yield p, sz, typ
        p += sz

def find(d, start, end, path):
    """Walk a box path, e.g. ('moov','trak','mdia'). Returns (offset, size)."""
    for p, sz, typ in boxes(d, start, end):
        if typ == path[0].encode():
            if len(path) == 1: return p, sz
            hit = find(d, p+8, p+sz, path[1:])
            if hit: return hit
    return None

def moov_span(path):
    """Offset and size of the moov box, by seeking the top-level boxes.

    Cameras and ffmpeg both write moov at the end, so this avoids pulling
    gigabytes of mdat through memory to reach a few kilobytes of tables.
    """
    with open(path,'rb') as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8: return None
            sz = struct.unpack(">I", hdr[:4])[0]; typ = hdr[4:8]
            off = f.tell() - 8
            if sz == 1: sz = struct.unpack(">Q", f.read(8))[0]
            if sz < 8: return None
            if typ == b'moov': return off, sz
            f.seek(off + sz)

def read_moov(path):
    """-> (offset in the file, the box as a mutable buffer)"""
    span = moov_span(path)
    if not span: sys.exit(f"no moov in {path} - is it an MP4/MOV?")
    with open(path,'rb') as f:
        f.seek(span[0]); return span[0], bytearray(f.read(span[1]))

def write_moov(path, off, buf):
    """moov is the last box, so a patched one is simply written over the tail."""
    with open(path,'r+b') as f:
        f.seek(off); f.write(buf); f.truncate()

def parameter_sets(ref):
    """SPS/PPS sit ready-made in the reference's avcC box - no decoding needed."""
    _, d = read_moov(ref)

    stsd = find(d, 8, len(d), ('trak','mdia','minf','stbl','stsd'))
    i = d.find(b'avcC', stsd[0] if stsd else 0)
    if i < 0: sys.exit(f"no avcC in {ref} - not H.264?")
    p = i + 4 + 5                          # version, profile, compat, level, lengthSize
    def records(p, n):
        out = []
        for _ in range(n):
            ln = struct.unpack(">H", d[p:p+2])[0]
            out.append(d[p+2:p+2+ln]); p += 2 + ln
        return out, p
    sps, p = records(p+1, d[p] & 0x1f)
    pps, _ = records(p+1, d[p])
    if not sps or not pps: sys.exit(f"empty avcC in {ref}")
    return sps, pps

# ---------------------------------------------------------------- carved file

def walk(d, start):
    """Follow the AVCC length-prefix chain from `start`. Returns (nals, end)."""
    p, out = start, []
    while p + 5 <= len(d):
        ln = struct.unpack(">I", d[p:p+4])[0]
        h = d[p+4]
        if h & 0x80: break                            # forbidden_zero_bit must be 0
        if ln <= 0 or ln > NAL_LIMITS.get(h & 0x1f, 0) or p+4+ln > len(d): break
        out.append((p+4, ln))
        p += 4 + ln
    return out, p

def segments(d, min_nals=5):
    """Video chunks, skipping the audio and metadata chunks between them."""
    segs, p = [], 0
    while p < len(d) - 8:
        nals, end = walk(d, p)
        if len(nals) >= min_nals:
            segs.append((p, end)); p = end
            continue
        q = d.find(CHUNK_START, p + 1)
        while q >= 0 and len(walk(d, q)[0]) < min_nals:
            q = d.find(CHUNK_START, q + 1)
        if q < 0: break
        p = q
    return segs

def chunk_nals(d, a):
    """The NALs of one video chunk, starting on an access unit boundary.

    A resync can land in the middle of a frame; the orphan slices ahead of the
    next access unit delimiter would then be counted as a frame of their own.
    """
    mv = memoryview(d)                    # views, not copies: the payload is huge
    ns = [n for o, l in walk(d, a)[0] if not NOT_A_NAL.search(n := mv[o:o+l])]
    for i, n in enumerate(ns[:16]):
        if n[0] & 0x1f == 9: return ns[i:]         # access unit delimiter
    return ns

def audio_from_gaps(d, segs):
    """LPCM sits at the head of every gap, ahead of the KLV metadata track.

    Where a whole run of chunks was never recovered the gap is tens of megabytes
    and its first KLV marker is far too deep to use as the cut, so the chunk
    length that repeats across the file caps it.
    """
    gaps = [(segs[i][1], segs[i+1][0]) for i in range(len(segs)-1)]
    found = []
    for a, b in gaps:
        k = d[a:b].find(KLV)
        if k > AUDIO_HDR: found.append(k - AUDIO_HDR)
    if not found: return b''
    usual = collections.Counter(found).most_common(1)[0][0]
    out = b''
    for a, b in gaps:
        k = d[a:b].find(KLV)
        n = min(k - AUDIO_HDR, usual) if k > AUDIO_HDR else usual
        out += d[a:a+min(n, b-a)]
    return out

# ---------------------------------------------------------------- mp4 surgery

def patch_ctts(path, offsets, frame_ticks):
    """Insert a composition-offset table into the video track.

    moov sits behind mdat here, so growing it leaves every stco chunk offset
    untouched - only the ancestor box sizes need fixing. Offsets are shifted to
    be non-negative so a version-0 table works everywhere; the same shift then
    goes into the edit list, which is exactly how the camera writes it.
    """
    moov_off, d = read_moov(path)
    moov = (0, len(d))
    chain = trak = None
    for p, sz, typ in boxes(d, moov[0]+8, moov[0]+moov[1]):
        if typ != b'trak': continue
        stbl = find(d, p+8, p+sz, ('mdia','minf','stbl'))
        if stbl and d[stbl[0]:stbl[0]+stbl[1]].find(b'avc1') > 0:
            trak = (p, sz)
            chain = [moov, trak, find(d, p+8, p+sz, ('mdia',)),
                     find(d, p+8, p+sz, ('mdia','minf')), stbl]
            break
    if not chain: sys.exit("no avc1 track")
    stbl = chain[-1]
    stsz = find(d, stbl[0]+8, stbl[0]+stbl[1], ('stsz',))
    n = struct.unpack(">I", d[stsz[0]+16:stsz[0]+20])[0]
    if n != len(offsets):
        sys.exit(f"sample count {n} != {len(offsets)} frames - refusing to patch")

    shift = max(0, -min(offsets))
    runs = []
    for o in offsets:
        o += shift
        if runs and runs[-1][1] == o: runs[-1][0] += 1
        else: runs.append([1, o])
    body = b''.join(struct.pack(">II", c, o*frame_ticks) for c, o in runs)
    ctts = struct.pack(">I", 16+len(body)) + b'ctts' + b'\x00\x00\x00\x00' \
           + struct.pack(">I", len(runs)) + body

    # The edit list has to start at the earliest composition time and run the
    # full length, otherwise the first and last displayed frames get trimmed.
    # ffmpeg sized it while the file still had no ctts, so it is off by the
    # B-frame delay at both ends.
    elst = find(d, trak[0]+8, trak[0]+trak[1], ('edts','elst'))
    if elst and struct.unpack(">I", d[elst[0]+12:elst[0]+16])[0] == 1:
        mvhd = find(d, moov[0]+8, moov[0]+moov[1], ('mvhd',))
        mdhd = find(d, trak[0]+8, trak[0]+trak[1], ('mdia','mdhd'))
        movie_ts = struct.unpack(">I", d[mvhd[0]+20:mvhd[0]+24])[0]
        media_ts = struct.unpack(">I", d[mdhd[0]+20:mdhd[0]+24])[0]
        struct.pack_into(">I", d, elst[0]+16, n*frame_ticks*movie_ts//media_ts)
        struct.pack_into(">i", d, elst[0]+20, shift*frame_ticks)

    stts = find(d, stbl[0]+8, stbl[0]+stbl[1], ('stts',))
    at = stts[0] + stts[1]                            # ctts belongs right after stts
    d[at:at] = ctts
    for off, sz in chain:                             # grow every ancestor
        struct.pack_into(">I", d, off, sz + len(ctts))
    write_moov(path, moov_off, d)
    return len(runs), shift

def set_rotation(path, deg):
    """Write the display matrix into tkhd.

    Rotation is the one thing here a player cannot infer from the bitstream: the
    frames are always stored landscape, and only the matrix says the camera was
    held upright. Shoot vertically and the recovered clip comes out on its side.
    `deg` is the clockwise rotation a player should apply, as the camera writes it.
    """
    deg %= 360
    if deg == 0: return 0
    moov_off, d = read_moov(path)
    moov = (0, len(d))
    tkhd = None
    for p, sz, typ in boxes(d, moov[0]+8, moov[0]+moov[1]):
        if typ != b'trak': continue
        stbl = find(d, p+8, p+sz, ('mdia','minf','stbl'))
        if stbl and d[stbl[0]:stbl[0]+stbl[1]].find(b'avc1') > 0:
            tkhd = find(d, p+8, p+sz, ('tkhd',)); break
    if not tkhd: sys.exit("no video tkhd")
    base = tkhd[0] + (60 if d[tkhd[0]+8] == 1 else 48)
    w, h = (struct.unpack(">I", d[base+36+i*4:base+40+i*4])[0] for i in range(2))
    ONE = 0x10000
    m = {90:  (0, ONE, 0, -ONE, 0, 0, h, 0),
         180: (-ONE, 0, 0, 0, -ONE, 0, w, h),
         270: (0, -ONE, 0, ONE, 0, 0, 0, w)}.get(deg)
    if not m: sys.exit(f"rotation {deg} must be 0, 90, 180 or 270")
    for i, v in enumerate(m):
        struct.pack_into(">i", d, base+i*4, v)
    struct.pack_into(">i", d, base+32, 0x40000000)
    write_moov(path, moov_off, d)
    return deg

# ---------------------------------------------------------------- main

def rebuild(broken, sps, pps, out, fps=25, rot=0, log=print):
    """The whole job, given parameter sets rather than a reference file.

    Returns (frames kept, seconds of audio, frames dropped at the head).
    """
    d = open(broken,'rb').read()
    segs = segments(d)
    if not segs: sys.exit("no H.264 chain found - not carved XAVC payload")

    nals = [n for a, _ in segs for n in chunk_nals(d, a)]
    au = access_units(nals, *parse_sps(sps[0]))
    if not au: sys.exit("no frames found - wrong reference clip?")

    # NAL range of each access unit, its leading AUD/SEI included
    starts = []
    for i, _, _ in au:
        lo = i
        while lo > 0 and nals[lo-1][0] & 0x1f in (6, 9): lo -= 1
        starts.append(lo)
    starts.append(len(nals))
    def slices(k):
        return sum(1 for n in nals[starts[k]:starts[k+1]] if (n[0] & 0x1f) in (1, 5))

    # Start at the first IDR - everything before it references a GOP that is gone.
    # Sony records open GOPs, so the IDR is followed by leading pictures with a
    # negative POC that display ahead of it and lean on that same lost GOP; they
    # are non-reference frames, so dropping them costs nothing else.
    first = next((k for k, (_, isidr, _) in enumerate(au) if isidr), 0)
    nxt = next((k for k in range(first+1, len(au)) if au[k][1]), len(au))
    leading = {k for k in range(first+1, nxt) if au[k][2] < 0}
    keep = [k for k in range(first, len(au)) if k not in leading]

    # A frame is split across a fixed number of slices. The carve stops mid-frame,
    # and a resync can drop into one, so any access unit short of that count is a
    # fragment no decoder can turn into a picture.
    counts = [slices(k) for k in range(len(au))]
    full = collections.Counter(counts).most_common(1)[0][0]
    partial = [k for k in keep if counts[k] != full]
    keep = [k for k in keep if counts[k] == full]

    au = [au[k] for k in keep]
    offsets = composition_offsets(au)
    head = first + len(leading)

    es = out + ".h264"
    with open(es,'wb') as f:
        f.write(b''.join(b'\x00\x00\x00\x01'+x for x in sps+pps))
        for k in keep:
            lo, hi = starts[k], starts[k+1]
            # a delimiter or SEI always precedes slices, so trailing ones are
            # orphans of a dropped frame - ffmpeg would count them as a picture
            while hi > lo and nals[hi-1][0] & 0x1f in (6, 9): hi -= 1
            for n in nals[lo:hi]: f.write(b'\x00\x00\x00\x01' + n)

    pcm = audio_from_gaps(d, segs)[head * AUDIO_RATE // fps:]   # keep A/V in sync
    # A chunk that the carve only returned in part still leaves a full audio chunk
    # in the gap behind it, so on a badly fragmented file the sound outruns the
    # picture. Never let it play past the last frame.
    pcm = pcm[:len(au) * AUDIO_RATE // fps]
    pcm = pcm[:len(pcm) // 4 * 4]                               # whole stereo frames

    # Everything below works on files, not on the payload, and ffmpeg is about to
    # want memory of its own. Keep the numbers, drop the gigabytes.
    stats = (len(segs), len(nals), sum(b - a for a, b in segs) / len(d) * 100)
    del d, nals, segs, starts

    cmd = ["ffmpeg","-v","error","-r",str(fps),"-i",es]
    if pcm:
        ap = out + ".pcm"; open(ap,'wb').write(pcm)
        cmd += ["-f","s16be","-ar","48000","-ac","2","-i",ap,
                "-c:a","pcm_s16be","-map","0:v","-map","1:a"]
    cmd += ["-c:v","copy","-y",out]
    subprocess.run(cmd, check=True)
    os.unlink(es)
    if pcm: os.unlink(ap)

    ticks = int(subprocess.run(["ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=time_base","-of","csv=p=0",out],
        capture_output=True, text=True).stdout.strip().split('/')[1]) // fps
    try:
        runs, shift = patch_ctts(out, offsets, ticks)
    except SystemExit:
        os.unlink(out)                 # never leave a half-fixed file behind
        raise

    log(f"{stats[0]} video chunks, {stats[1]} NALs, "
        f"{stats[2]:.1f}% of the file is video payload")
    log(f"dropped {first} frames before the first IDR, "
          f"{len(leading)} open-GOP leading frames"
          f"{f', {len(partial)} partial frames' if partial else ''}; kept {len(au)}")
    log(f"audio: {len(pcm)} bytes LPCM = {len(pcm)/AUDIO_RATE:.2f} s")
    log(f"ctts: {runs} runs patched in, +{shift} frame shift ({ticks} ticks/frame)")
    if set_rotation(out, rot): log(f"rotation: {rot} deg clockwise written to tkhd")
    log(f"wrote {out}")
    return len(au), len(pcm) / AUDIO_RATE, head

def main():
    broken, ref, out = sys.argv[1], sys.argv[2], sys.argv[3]
    fps = int(sys.argv[4]) if len(sys.argv) > 4 else 25
    rot = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    sps, pps = parameter_sets(ref)
    rebuild(broken, sps, pps, out, fps, rot)

if __name__ == "__main__":
    main()
