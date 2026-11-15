"""Dočasná sonda: proč EirGrid z běhu hlídače vrací 503. Smaže se po ověření."""
import json, sys, urllib.request, urllib.error
from datetime import datetime, timezone
sys.path.insert(0, ".")
import hlidac
d = datetime.now(timezone.utc)
ds = f"{d.day:02d}-{hlidac.MONTHS_EN[d.month - 1]}-{d.year}"
url = f"{hlidac.EIRGRID}?area=demandactual&region=ROI&datefrom={ds}+00%3A00&dateto={ds}+23%3A59"
variants = {
    "hlidac": {"User-Agent": hlidac.UA, "Accept": "*/*"},
    "bez_accept": {"User-Agent": hlidac.UA},
    "accept_json": {"User-Agent": hlidac.UA, "Accept": "application/json"},
    "sonda_ua": {"User-Agent": "KrizovyHlidac-sonda/1.0 (+https://github.com/)"},
    "prohlizec": {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept": "application/json, text/plain, */*"},
}
out = {"url": url}
for k, h in variants.items():
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
            out[k] = f"{r.status} {len(r.read())} B"
    except urllib.error.HTTPError as e:
        out[k] = f"HTTP {e.code} {e.read(200)!r}"
    except Exception as e:
        out[k] = f"{type(e).__name__}: {e}"
print(json.dumps(out, ensure_ascii=False, indent=1))
