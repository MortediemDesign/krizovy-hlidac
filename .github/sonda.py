"""Dočasná sonda: proč kanál radiace BM OKF občas nemá měření. Smaže se po ověření."""
import json, re, sys, time, urllib.request
sys.path.insert(0, ".")
import hlidac
out = []
for i in range(16):
    r = {"i": i, "cas": time.strftime("%H:%M:%S")}
    try:
        raw = hlidac.http_fetch(hlidac.OKF_SUGARZAS, timeout=25)
        t = raw.decode("utf-8", "replace")
        r["delka"] = len(raw)
        r["polozek"] = t.count("<item")
        r["nsv"] = len(re.findall(r"nsv/h", t, re.I))
        try:
            hlidac.src_radiation_hu({"radiace_hu": {}}, lambda u, **k: raw)
            r["ok"] = True
        except Exception as e:
            r["ok"] = False
            r["chyba"] = f"{type(e).__name__}: {e}"
            r["zacatek"] = t[:1500]
            r["polozky"] = re.findall(r"<item>.*?</item>", t, re.S)[:3]
            r["konec"] = t[-400:]
    except Exception as e:
        r["fetch"] = f"{type(e).__name__}: {e}"
    out.append(r)
    time.sleep(25)
print(json.dumps(out, ensure_ascii=False, indent=1))
