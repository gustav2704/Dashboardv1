import os, re, glob

local = os.path.join(os.path.dirname(__file__), "..", "data", "chr_probe")
UTF16 = re.compile(rb"(?:[\x20-\x7e\x80-\xff]\x00)+")

PERIODS = {1:"M1",2:"M2",3:"M3",4:"M4",5:"M5",6:"M6",10:"M10",12:"M12",15:"M15",
           20:"M20",30:"M30",60:"H1",120:"H2",180:"H3",240:"H4",360:"H6",480:"H8",
           720:"H12",1440:"D1",10080:"W1",43200:"MN1",16385:"TICKS"}

def parse(path):
    with open(path,"rb") as fh: data=fh.read()
    strings=[s.decode("utf-16le","ignore") for s in UTF16.findall(data)]
    def get(name):
        p=name+"="
        return next((v[len(p):].strip() for v in strings if v.startswith(p)),"")
    return {
        "file": os.path.basename(path),
        "symbol": get("symbol"),
        "period_type": get("period_type"),
        "period_size": get("period_size"),
        "expert_path": get("path"),
        "expert_name": get("name"),
        "magic": get("MagicNumber") or get("MyNumber"),
        "comment": get("CustomComment"),
    }

for p in sorted(glob.glob(os.path.join(local,"*.chr"))):
    d=parse(p)
    pt=d["period_type"]; ps=d["period_size"]
    tf=PERIODS.get(int(ps)) if ps.isdigit() else ps
    exp=os.path.basename(d["expert_path"]) if d["expert_path"] else ""
    print(f"{d['file']:<10} {d['symbol']:<10} pt={pt} ps={ps:<6} TF={tf!r:<6} magic={d['magic']:<9} exp={exp!r} comment={d['comment']!r}")
