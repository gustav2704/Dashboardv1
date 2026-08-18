import os, re, glob

local = os.path.join(os.path.dirname(__file__), "..", "data", "chr_probe")
UTF16 = re.compile(rb"(?:[\x20-\x7e\x80-\xff]\x00)+")

def fields(path):
    with open(path, "rb") as fh:
        data = fh.read()
    strings = [s.decode("utf-16le", "ignore") for s in UTF16.findall(data)]
    return strings

# Probe chart01 (small, 26KB)
p = os.path.join(local, "chart01.chr")
strings = fields(p)
# Print all strings that look like key=value or interesting tokens
interesting = [s for s in strings if any(k in s.lower() for k in
    ["symbol","period","tf","timeframe","path","magic","chart","window","expert","comment"])]
print("=== chart01.chr interesting strings ===")
for s in interesting:
    print(repr(s))
print("\n=== all short strings (first 80) ===")
for s in strings[:80]:
    print(repr(s))
