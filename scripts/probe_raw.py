import os, re
BASE = os.path.join(os.path.dirname(__file__), "..", "data", "chr_all")
UTF16 = re.compile(rb"(?:[\x20-\x7e\x80-\xff]\x00)+")

def dump(rel):
    p = os.path.join(BASE, rel)
    with open(p, "rb") as fh: data = fh.read()
    strings = [s.decode("utf-16le","ignore") for s in UTF16.findall(data)]
    print(f"\n===== {rel} (len={len(data)}) =====")
    for s in strings:
        if any(k in s.lower() for k in ["symbol","period","tf","path","magic","comment","name=","<chart>","<expert>","<input"]):
            print("  ", repr(s[:120]))

dump(r"145\6CBF7564561DE9AB10D5A7C696F37CDC\chart22.chr")  # BB_GBPJPY_H4
dump(r"145\6CBF7564561DE9AB10D5A7C696F37CDC\chart19.chr")  # 3GreenCandles_H1
dump(r"145\9DE44EFC77A6A3ABBD348AFCCFB49D70\chart16.chr")  # DosVelas_eurusd_h4
dump(r"145\6CBF7564561DE9AB10D5A7C696F37CDC\chart14.chr")  # WF Matrix XAU
