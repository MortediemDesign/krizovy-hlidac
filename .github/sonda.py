"""Dočasná sonda: ověří z GitHubu, jak odpovídají státní zdroje pro útočiště. Smaže se po ověření."""
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
d = now.strftime("%Y-%m-%d")
dy = (now - timedelta(days=1)).strftime("%Y-%m-%d")
ymd = now.strftime("%Y%m%d")
ymd1 = (now - timedelta(days=1)).strftime("%Y%m%d")
ie = now.strftime("%d-%b-%Y")
d14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")

UA = "KrizovyHlidac-sonda/1.0 (+https://github.com/)"
J = {"Accept": "application/json"}
URLS = [
    ("epa_rad_doc", "https://data.epa.ie/api-list/radiation-monitoring-open-data/", None),
    ("ren_daily_y", f"https://servicebus.ren.pt/datahubapi/electricity/ElectricityConsumptionSupplyDaily?culture=pt-PT&date={dy}", None),
    ("ren_home", "https://datahub.ren.pt/pt/", "JS"),
    ("radnet", "https://siradgeo.apambiente.pt/monitorizacao/emergencia/visao-global", "JS"),
    ("anepc_q", "https://prociv-agserver.geomai.mai.gov.pt/arcgis/rest/services/Ocorrencias_Base/FeatureServer/0/query?where=1%3D1&outFields=*&f=json&resultRecordCount=3", "MOZ"),
    ("epa_gdr", "https://epawebapp.epa.ie/Article27Register/GammaDoseRateNew.aspx", "JS"),
]

HINT = re.compile(r"""(?:href|src|action)=["']([^"']*(?:rss|feed|api|json|radmon|xml|service|arcgis)[^"']*)["']""", re.I)
URLISH = re.compile(r"""https?://[^\s"'<>)]*(?:api|json|rss|feed|radmon|Service|arcgis)[^\s"'<>)]*""", re.I)

out = {"cas": now.isoformat(), "vysledky": {}}
for key, url, hdr in URLS:
    r = {"url": url}
    try:
        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"} if hdr in ("MOZ", "JS") else {"User-Agent": UA, **(hdr or {})}
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(3_000_000)
            r["status"] = resp.status
            r["ctype"] = resp.headers.get("Content-Type")
            r["final"] = resp.geturl()
    except urllib.error.HTTPError as e:
        raw = e.read(20000) if hasattr(e, "read") else b""
        r["status"] = e.code
        r["ctype"] = e.headers.get("Content-Type") if e.headers else None
    except Exception as e:
        r["chyba"] = f"{type(e).__name__}: {e}"[:300]
        out["vysledky"][key] = r
        continue
    txt = raw.decode("utf-8", "replace")
    r["delka"] = len(raw)
    r["zacatek"] = txt[:1500]
    if len(txt) > 1500:
        r["konec"] = txt[-600:]
    hints = []
    for m in HINT.findall(txt) + URLISH.findall(txt):
        if m not in hints:
            hints.append(m)
    r["odkazy"] = hints[:40]
    # JSON: popiš klíče
    try:
        js = json.loads(txt)
        def shape(o, depth=0):
            if depth > 4:
                return "…"
            if isinstance(o, dict):
                return {k: shape(v, depth + 1) for k, v in list(o.items())[:25]}
            if isinstance(o, list):
                return [f"len={len(o)}", shape(o[0], depth + 1)] if o else []
            return repr(o)[:80]
        r["tvar"] = shape(js)
    except Exception:
        pass
    if hdr == "JS":
        import urllib.parse
        api = set()
        for src in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", txt)[:15]:
            full = urllib.parse.urljoin(r.get("final") or url, src)
            try:
                q = urllib.request.Request(full, headers=h)
                with urllib.request.urlopen(q, timeout=30) as rs:
                    js = rs.read(6_000_000).decode("utf-8", "replace")
                for m in re.findall(r"""["'`]([^"'`\s]*(?:api|Api|API|service|Service|json|arcgis|rest/)[^"'`\s]{0,120})["'`]""", js):
                    if len(m) > 6 and not m.endswith((".css", ".png", ".svg")):
                        api.add(m[:160])
            except Exception as e:
                api.add(f"!{full}: {type(e).__name__}")
        r["js_api"] = sorted(api)[:150]
    out["vysledky"][key] = r

print(json.dumps(out, ensure_ascii=False, indent=1))
