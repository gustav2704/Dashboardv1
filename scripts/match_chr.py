import os, re, json

BASE = os.path.join(os.path.dirname(__file__), "..", "data")
missing = json.load(open(os.path.join(BASE, "missing_tf_strategies.json"), encoding="utf-8"))
records = json.load(open(os.path.join(BASE, "chr_all", "all_chr.json"), encoding="utf-8"))

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

for r in records:
    r["expert_n"] = norm(r["expert"])
    r["comment_n"] = norm(r["comment"])
    r["symbol_n"] = norm(r["symbol"])

# terminal -> known account map
TERM_ACCOUNT = {
    "1D8B0F5DE56F27A2A76D83BE69DE515E":"4000094894",
    "6C3C6A11D1C3791DD4DBF45421BF8028":"3000097316",
    "6FBEE76C719DC78AB2AE839B5A0C7442":"10025867",
    "951377C0D4197CD328B0F10CBC4A6039":"7396577",
    "C59B2EBA0DCBEDCCBFE7CA7E011A3F88":"3000097316",
    "CC3A30E70B6FDF563DFA143DA3E22AD5":"7396577",
    "13BD90F6BE9FAA167ECD83CA4C4F9A5D":"7396577",
    "169BA68A25A48540472B316FB50A25B8":"7396577",
    "455ED109B802E41AC490CBECDE40FE93":"7396577",
}
for r in records:
    r["acct"] = TERM_ACCOUNT.get(r["term"]) or r["acct_from_path"]

results=[]
for s in missing:
    n_mql = s["n_mql5"]; n_sqx = s["n_sqx"]
    sym_n = norm(s["symbol"])
    acct = norm(s["account"])
    hits=[]
    for r in records:
        if not (r["expert_n"] or r["comment_n"]):
            continue
        en=r["expert_n"]; cn=r["comment_n"]
        # strong: exact name match on expert or comment
        strong = (n_mql and (en==n_mql or cn==n_mql)) or (n_sqx and (en==n_sqx or cn==n_sqx))
        # containment
        contains = False
        for nm in (n_mql, n_sqx):
            if not nm: continue
            if nm in en or nm in cn or en in nm or cn in nm:
                contains=True; break
        if not (strong or contains):
            continue
        # account preference (if strategy has account)
        acct_ok = (not acct) or (norm(r["acct"])==acct)
        hits.append({**r, "strong":strong, "acct_ok":acct_ok})
    if hits:
        # prefer account-ok and strong
        hits.sort(key=lambda h: (not h["acct_ok"], not h["strong"]))
        results.append({"strategy":s, "hits":hits})

print(f"strategies with >=1 chr hit: {len(results)}")
for res in results:
    s=res["strategy"]
    print(f"\nSTRAT id={s['id']} {s['mql5'] or s['sqx']!r} acct={s['account']!r} server={s['server']!r} sym={s['symbol']!r}")
    for h in res["hits"][:4]:
        print(f"   {h['tag']}/{h['term'][:8]} {h['file']} sym={h['symbol']!r} TF={h['timeframe']!r} acct={h['acct']!r} strong={h['strong']} acct_ok={h['acct_ok']}")
        print(f"      expert={h['expert']!r}")
        print(f"      comment={h['comment']!r}")
