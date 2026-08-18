import os, re, json, glob

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "chr_all")
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
        "symbol": get("symbol"),
        "period_type": get("period_type"),
        "period_size": get("period_size"),
        "expert_path": get("path"),
        "magic": get("MagicNumber") or get("MyNumber"),
        "comment": get("CustomComment"),
    }

records=[]
for tag in ("145","100"):
    tagbase=os.path.join(BASE,tag)
    if not os.path.isdir(tagbase): continue
    for term in sorted(os.listdir(tagbase)):
        tdir=os.path.join(tagbase,term)
        if not os.path.isdir(tdir): continue
        for f in sorted(os.listdir(tdir)):
            if not f.lower().endswith(".chr"): continue
            d=parse(os.path.join(tdir,f))
            ps=d["period_size"]
            tf=PERIODS.get(int(ps)) if ps.isdigit() else ps
            expert=os.path.basename(d["expert_path"]) if d["expert_path"] else ""
            # try to extract account from expert path folder
            acct=None
            if d["expert_path"]:
                parts=[p for p in d["expert_path"].replace("\\","/").split("/")]
                for part in parts[:-1]:
                    m=re.search(r'(\d{5,10})', part)
                    if m:
                        acct=m.group(1); break
            records.append({
                "tag":tag, "term":term, "file":f,
                "symbol":d["symbol"], "period_type":d["period_type"],
                "period_size":ps, "timeframe":tf,
                "expert":expert, "comment":d["comment"],
                "magic":d["magic"], "acct_from_path":acct,
            })

out=os.path.join(BASE, "all_chr.json")
with open(out,"w",encoding="utf-8") as fh:
    json.dump(records,fh,ensure_ascii=False,indent=2)
print("total chr records:", len(records))
# quick summary of distinct experts
from collections import Counter
c=Counter(r["expert"] for r in records if r["expert"])
print("distinct experts:", len(c))
