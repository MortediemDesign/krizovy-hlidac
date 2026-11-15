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
    ("ree_rt", f"https://apidatos.ree.es/es/datos/demanda/demanda-tiempo-real?start_date={d}T00:00&end_date={d}T23:59&time_trunc=hour", None),
    ("boe_dnes", f"https://www.boe.es/datosabiertos/api/boe/sumario/{ymd}", J),
    ("boe_vcera", f"https://www.boe.es/datosabiertos/api/boe/sumario/{ymd1}", J),
    ("incibe", "https://www.incibe.es/incibe-cert/alerta-temprana/avisos/feed", None),
    ("dsn_rss1", "https://www.dsn.gob.es/es/rss.xml", None),
    ("dsn_home", "https://www.dsn.gob.es/es", None),
    ("ign_sismo", "https://www.ign.es/ign/RssTools/sismologia.xml", None),
    ("pcivil_home", "https://www.proteccioncivil.es/", None),
    ("anepc_q", "https://prociv-agserver.geomai.mai.gov.pt/arcgis/rest/services/Ocorrencias_Base/FeatureServer/0/query?where=1%3D1&outFields=*&f=json&resultRecordCount=3", None),
    ("anepc_srv", "https://prociv-agserver.geomai.mai.gov.pt/arcgis/rest/services?f=json", None),
    ("fogos_act", "https://api.fogos.pt/v2/incidents/active?all=1", None),
    ("fogos_new", "https://api.fogos.pt/new/fires", None),
    ("prociv_home", "https://prociv.gov.pt/pt/", None),
    ("ren_daily", f"https://servicebus.ren.pt/datahubapi/electricity/ElectricityConsumptionSupplyDaily?culture=pt-PT&date={d}", None),
    ("ren_home", "https://datahub.ren.pt/pt/", None),
    ("dre_home", "https://diariodarepublica.pt/dr/home", None),
    ("dre_rss1", "https://diariodarepublica.pt/dr/rss/serie1", None),
    ("cncs_alertas", "https://www.cncs.gov.pt/pt/alertas/", None),
    ("ipma_sismo", "https://api.ipma.pt/open-data/observation/seismic/7.json", None),
    ("radnet_home", "https://radnet.apambiente.pt/", None),
    ("eir_act", f"https://www.smartgriddashboard.com/DashboardService.svc/data?area=demandactual&region=ROI&datefrom={ie}+00%3A00&dateto={ie}+23%3A59", None),
    ("eir_fc", f"https://www.smartgriddashboard.com/DashboardService.svc/data?area=demandforecast&region=ROI&datefrom={ie}+00%3A00&dateto={ie}+23%3A59", None),
    ("eir_home", "https://www.smartgriddashboard.com/", None),
    ("oireachtas", f"https://api.oireachtas.ie/v1/legislation?date_start={d14}&limit=3", None),
    ("ncsc_ie", "https://www.ncsc.gov.ie/news/alerts.rss", None),
    ("epa_apis", "https://data.epa.ie/api-list/", None),
    ("epa_radmon", "https://data.epa.ie/radmon/", None),
    ("iris", "https://www.irisoifigiuil.ie/", None),
]

HINT = re.compile(r"""(?:href|src|action)=["']([^"']*(?:rss|feed|api|json|radmon|xml|service|arcgis)[^"']*)["']""", re.I)
URLISH = re.compile(r"""https?://[^\s"'<>)]*(?:api|json|rss|feed|radmon|Service|arcgis)[^\s"'<>)]*""", re.I)

out = {"cas": now.isoformat(), "vysledky": {}}
for key, url, hdr in URLS:
    r = {"url": url}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(hdr or {})})
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
    out["vysledky"][key] = r

print(json.dumps(out, ensure_ascii=False, indent=1))
