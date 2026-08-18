import os, re, json

BASE = os.path.join(os.path.dirname(__file__), "..", "data")
missing = json.load(open(os.path.join(BASE, "missing_tf_strategies.json"), encoding="utf-8"))
records = json.load(open(os.path.join(BASE, "chr_all", "all_chr.json"), encoding="utf-8"))

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

TF_RE = re.compile(r'(M1|M2|M3|M5|M10|M15|M20|M30|H1|H2|H4|H6|H12|D1|W1|MN1)\b', re.I)
# Also _m15 / _m5 / _h1 / _h4 etc with underscore
TF_RE2 = re.compile(r'[_\-](m15|m5|m30|m1|h1|h4|h2|d1|w1|m10|m20|h6|h12|mn1)\b', re.I)

def tf_from_name(name):
    m = TF_RE.search(name or "")
    if m:
        return m.group(1).upper()
    m = TF_RE2.search(name or "")
    if m:
        return m.group(1).upper()
    return None

# group records by (comment_n, expert_n, symbol_n)
TERM_ACCOUNT = {
    "1D8B0F5DE56F27A2A76D83BE69DE515E":"4000094894","6C3C6A11D1C3791DD4DBF45421BF8028":"3000097316",
    "6FBEE76C719DC78AB2AE839B5A0C7442":"10025867","951377C0D4197CD328B0F10CBC4A6039":"7396577",
    "C59B2EBA0DCBEDCCBFE7CA7E011A3F88":"3000097316","CC3A30E70B6FDF563DFA143DA3E22AD5":"7396577",
    "13BD90F6BE9FAA167ECD83CA4C4F9A5D":"7396577","169BA68A25A48540472B316FB50A25B8":"7396577",
    "455ED109B802E41AC490CBECDE40FE93":"7396577",
}

rows=[]
for s in missing:
    n_mql = s["n_mql5"]; n_sqx = s["n_sqx"]
    name_tf = tf_from_name(s["mql5"]) or tf_from_name(s["sqx"])
    # find chr hits (strong first)
    hits=[]
    for r in records:
        en=r.get("expert_n") or norm(r.get("expert")); cn=r.get("comment_n") or norm(r.get("comment"))
        if not (en or cn): continue
        strong = (n_mql and (en==n_mql or cn==n_mql)) or (n_sqx and (en==n_sqx or cn==n_sqx))
        contains=False
        for nm in (n_mql,n_sqx):
            if nm and (nm in en or nm in cn or en in nm or cn in nm): contains=True; break
        if strong or contains:
            acct_ok = (not norm(s["account"])) or (norm(r.get("acct") or "")==norm(s["account"]))
            hits.append((r, strong, acct_ok))
    hits.sort(key=lambda h: (not h[2], not h[1]))
    chr_tf = None; chr_expert=None; chr_acct=None
    for r,strong,acct_ok in hits[:1]:
        if r.get("timeframe"): chr_tf=r["timeframe"]; chr_expert=r.get("expert"); chr_acct=r.get("acct")
    # if chart period is M1/M4 and name has a bigger TF, prefer name (chart is a proxy)
    prefer_name = name_tf and chr_tf in ("M1","M2","M3","M4","M6") 
    final_tf = name_tf or chr_tf
    rows.append({
        "id": s["id"], "name": s["mql5"] or s["sqx"], "account": s["account"], "server": s["server"],
        "symbol": s["symbol"], "origin": s["origin"],
        "name_tf": name_tf, "chr_tf": chr_tf, "chr_expert": chr_expert, "chr_acct": chr_acct,
        "final_tf": final_tf,
    })

# save
with open(os.path.join(BASE,"tf_resolution.json"),"w",encoding="utf-8") as fh:
    json.dump(rows,fh,ensure_ascii=False,indent=2)

# summary
has = [r for r in rows if r["final_tf"]]
no = [r for r in rows if not r["final_tf"]]
print(f"TOTAL missing: {len(rows)} | resolvable: {len(has)} | no-TF-evidence: {len(no)}")
print("\n=== RESOLVED ===")
for r in sorted(has, key=lambda x:(x["server"],x["account"],x["name"])):
    src = "name" if r["name_tf"] and r["final_tf"]==r["name_tf"] else "chr"
    print(f"  id={r['id']:<4} TF={r['final_tf']:<4}[{src}] acct={r['account']!r:<10} {r['name']!r}  (chr_tf={r['chr_tf']})")
print("\n=== NO TF EVIDENCE ===")
for r in sorted(no, key=lambda x:(x["server"],x["account"],x["name"])):
    print(f"  id={r['id']:<4} acct={r['account']!r:<10} {r['name']!r}  (chr_expert={r['chr_expert']})")
