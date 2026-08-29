"""Container-level check: navigate the box tree (never byte-search - a big mdat
contains random 'ctts'/'stts' matches)."""
import struct, subprocess, sys

def boxes(d, s, e):
    p = s
    while p+8 <= e:
        sz = struct.unpack(">I", d[p:p+4])[0]; typ = d[p+4:p+8]
        if sz == 1: sz = struct.unpack(">Q", d[p+8:p+16])[0]
        if sz < 8: return
        yield p, sz, typ; p += sz

def find(d, s, e, path):
    for p, sz, typ in boxes(d, s, e):
        if typ == path[0].encode():
            if len(path) == 1: return p, sz
            hit = find(d, p+8, p+sz, path[1:])
            if hit: return hit
    return None

f = sys.argv[1]
d = open(f,'rb').read()
moov = find(d, 0, len(d), ('moov',))
for p, sz, typ in boxes(d, moov[0]+8, moov[0]+moov[1]):
    if typ != b'trak': continue
    stbl = find(d, p+8, p+sz, ('mdia','minf','stbl'))
    if not (stbl and d[stbl[0]:stbl[0]+stbl[1]].find(b'avc1') > 0): continue
    print(f"  video track:")
    elst = find(d, p+8, p+sz, ('edts','elst'))
    if elst:
        n = struct.unpack(">I", d[elst[0]+12:elst[0]+16])[0]
        print("    elst:", [struct.unpack(">IiH", d[elst[0]+16+k*12:elst[0]+26+k*12]) for k in range(n)],
              " (duration, media_time, rate)")
    stts = find(d, stbl[0]+8, stbl[0]+stbl[1], ('stts',))
    n = struct.unpack(">I", d[stts[0]+12:stts[0]+16])[0]
    print("    stts:", [struct.unpack(">II", d[stts[0]+16+k*8:stts[0]+24+k*8]) for k in range(min(n,3))],
          "(count, delta)")
    ctts = find(d, stbl[0]+8, stbl[0]+stbl[1], ('ctts',))
    if not ctts: print("    ctts: MISSING"); break
    ver = d[ctts[0]+8]; n = struct.unpack(">I", d[ctts[0]+12:ctts[0]+16])[0]
    fmt = ">II" if ver == 0 else ">Ii"
    print(f"    ctts: v{ver}, {n} entries,",
          [struct.unpack(fmt, d[ctts[0]+16+k*8:ctts[0]+24+k*8]) for k in range(min(n,4))])
    break
