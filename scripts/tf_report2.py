import json, os
rows = json.load(open(os.path.join(os.path.dirname(__file__),"..","data","missing_tf_report.json"), encoding="utf-8"))
rows.sort(key=lambda x:(x["server"],x["account"],x["name"]))
for r in rows:
    print(f"[id={r['id']}] {r['name']}")
    print(f"    account={r['account']!r} server={r['server']!r} symbol={r['symbol']!r}")
    print(f"    origin={r['origin']!r} retired={r['retired']} confirmed_mappings={r['mappings']}")
    if r["evidence"]:
        for ev in r["evidence"]:
            tag,term,f,sym,tf,exp,strong = ev
            print(f"    chr-match({tag}/{term}/{f}): sym={sym} chartTF={tf} strong={strong} expert={exp!r}")
    else:
        print("    chr-match: none")
    print()
