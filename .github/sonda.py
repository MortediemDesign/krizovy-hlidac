"""Dočasná sonda: státní zdroje východního křídla. Smaže se po ověření."""
import json, re, sys, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
d = now.strftime("%Y-%m-%d")
d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
URLS = [
 ("ee_kriis_rss", "https://www.kriis.ee/rss-feeds/rss.xml"),
 ("lv_vestnesis_jl", "https://www.vestnesis.lv/feed/JL"),
 ("sk_mosr_rss", "https://www.mosr.sk/data/rss/aktuality.xml"),
 ("sk_vlada_rss", "https://www.vlada.gov.sk/rss/"),
 ("hu_honved_rss", "https://honvedelem.hu/rss.xml"),
 ("hu_katved_src", "https://www.katasztrofavedelem.hu/10198/rss-forrasok-a-bm-okf-oldalan"),
 ("hu_mk_feed", "https://magyarkozlony.hu/feed"),
 ("ro_transel", "https://www.transelectrica.ro/sen-filter"),
 ("lt_etar", "https://www.e-tar.lt/portal/lt/index"),
 ("lt_lrs", "https://www.lrs.lt/"),
 ("lt_kariuomene", "https://kariuomene.lt/"),
 ("lt_litgrid", "https://www.litgrid.eu/"),
 ("lt_lrt_rss", "https://www.lrt.lt/naujienos?rss"),
 ("lt_vrm", "https://vrm.lrv.lt/"),
 ("fi_finlex_list", "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/act/statute/list?format=json&page=1&limit=5&sortBy=dateIssued&startYear=2026&endYear=2026&langAndVersion=fin%40&typeStatute=act&categoryStatute=new-statute"),
 ("fi_eduskunta", "https://www.eduskunta.fi/FI/rss-syotteet/Sivut/default.aspx"),
 ("fi_raja", "https://raja.fi/"),
 ("fi_fmi_cap", "https://alerts.fmi.fi/cap/feed/atom_fi-FI.xml"),
 ("fi_yle_rss", "https://yle.fi/rss/uutiset/paauutiset"),
 ("pl_bbn", "https://www.bbn.gov.pl/"),
 ("pl_bbn_rss", "https://www.bbn.gov.pl/pl/rss"),
 ("pl_wp", "https://www.wojsko-polskie.pl/"),
 ("pl_eli_mp", "https://api.sejm.gov.pl/eli/acts/search?publisher=MP&pubDateFrom=2026-11-01&limit=3"),
 ("pl_eli_du7", "https://api.sejm.gov.pl/eli/acts/search?publisher=DU&pubDateFrom=2026-11-08&limit=500"),
 ("ro_mai_feed", "https://www.mai.gov.ro/feed/"),
 ("ro_dsu", "https://www.dsu.mai.gov.ro/"),
 ("ro_mapn_rss", "https://www.mapn.ro/rss.php"),
 ("ro_cdep", "https://www.cdep.ro/"),
 ("ro_sgg", "https://sgg.gov.ro/"),
 ("lv_sargs", "https://www.sargs.lv/"),
 ("lv_mod_rss", "https://www.mod.gov.lv/lv/rss.xml"),
 ("lv_ast", "https://www.ast.lv/"),
 ("md_parlament", "https://www.parlament.md/"),
 ("md_moldelectrica", "https://moldelectrica.md/"),
 ("ee_valitsus_rss", "https://valitsus.ee/rss.xml"),
 ("ee_kaitse_rss", "https://kaitseministeerium.ee/et/rss.xml"),
 ("ee_paaste_rss", "https://www.rescue.ee/rss"),
]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
LINK = re.compile(r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*>""", re.I)
HREF = re.compile(r"""href=["']([^"']*(?:rss|feed|atom|\.xml)[^"']*)["']""", re.I)

def one(item):
    key, url = item
    r = {"url": url}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html, application/xml, */*"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read(4_000_000); r["status"] = resp.status; r["ctype"] = resp.headers.get("Content-Type"); r["final"] = resp.geturl()
    except urllib.error.HTTPError as e:
        r["status"] = e.code; r["ctype"] = e.headers.get("Content-Type") if e.headers else None
        try: raw = e.read(3000)
        except Exception: raw = b""
    except Exception as e:
        r["chyba"] = f"{type(e).__name__}: {e}"[:200]; return key, r
    t = raw.decode("utf-8", "replace")
    r["delka"] = len(raw); r["zacatek"] = re.sub(r"\s+", " ", t[:700]) if key != "ro_transel" else t
    titles = re.findall(r"<title[^>]*>(.*?)</title>", t, re.S)
    if "<item" in t or "<entry" in t:
        r["polozek"] = t.count("<item") + t.count("<entry"); r["titulky"] = [re.sub(r"\s+", " ", x)[:120] for x in titles[:6]]
    feeds = []
    for m in LINK.findall(t):
        h = re.search(r"""href=["']([^"']+)""", m)
        if h: feeds.append(urllib.parse.urljoin(r.get("final") or url, h.group(1)))
    for h in HREF.findall(t):
        u = urllib.parse.urljoin(r.get("final") or url, h)
        if u not in feeds and not re.search(r"\.(css|js|png|svg|ico)(\?|$)|sitemap|xmlrpc|oembed|wp-json", u, re.I): feeds.append(u)
    r["kanaly"] = feeds[:25]
    try:
        js = json.loads(t)
        def shape(o, dp=0):
            if dp > 4: return "…"
            if isinstance(o, dict): return {k: shape(v, dp + 1) for k, v in list(o.items())[:30]}
            if isinstance(o, list): return [f"len={len(o)}", shape(o[0], dp + 1)] if o else []
            return repr(o)[:90]
        r["tvar"] = shape(js)
        if key == "pl_eli_openapi" or key == "ee_elering_docs":
            r["cesty"] = list(js.get("paths", {}))[:120]
            if key == "pl_eli_openapi":
                r["search_params"] = [p.get("name") for p in js["paths"].get("/acts/search", {}).get("get", {}).get("parameters", [])]
    except Exception:
        if key == "pl_eli_openapi":
            r["search_kus"] = t[t.find("/acts/search"): t.find("/acts/search") + 2500]
    return key, r

with ThreadPoolExecutor(max_workers=10) as ex:
    out = dict(ex.map(one, URLS))
print(json.dumps({"cas": now.isoformat(), "vysledky": out}, ensure_ascii=False, indent=1))
