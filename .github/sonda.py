"""Dočasná sonda: státní zdroje východního křídla. Smaže se po ověření."""
import json, re, sys, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
d = now.strftime("%Y-%m-%d")
d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
URLS = [
 # Polsko
 ("pl_eli_openapi", "https://api.sejm.gov.pl/eli/openapi"),
 ("pl_eli_search", f"https://api.sejm.gov.pl/eli/acts/search?publisher=DU&year=2026&limit=3&sortBy=promulgation&sortDir=desc"),
 ("pl_eli_year", "https://api.sejm.gov.pl/eli/acts/DU/2026"),
 ("pl_sejm_prints", "https://api.sejm.gov.pl/sejm/term10/prints?limit=3&sort_by=-documentDate"),
 ("pl_pse_root", "https://api.raporty.pse.pl/api/"),
 ("pl_pse_kse", f"https://api.raporty.pse.pl/api/kse-load?$filter=business_date%20eq%20%27{d}%27&$first=3"),
 ("pl_pse_hiswlk", f"https://api.raporty.pse.pl/api/his-wlk-cal?$filter=business_date%20eq%20%27{d}%27&$first=2"),
 ("pl_rcb", "https://www.gov.pl/web/rcb"),
 ("pl_rcb_rss", "https://www.gov.pl/web/rcb/rss"),
 ("pl_mon", "https://www.gov.pl/web/obrona-narodowa"),
 # Estonsko
 ("ee_elering_docs", "https://dashboard.elering.ee/v3/api-docs"),
 ("ee_elering_plan", f"https://dashboard.elering.ee/api/system/with-plan?start={d1}T00:00:00.000Z&end={d}T23:59:59.999Z"),
 ("ee_kriis", "https://www.kriis.ee/"),
 ("ee_valitsus", "https://valitsus.ee/en/news"),
 ("ee_kaitse", "https://kaitseministeerium.ee/en/news"),
 ("ee_paaste", "https://www.rescue.ee/"),
 ("ee_rt", "https://www.riigiteataja.ee/"),
 ("ee_riigikogu", "https://www.riigikogu.ee/en/feed/"),
 # Lotyšsko
 ("lv_mod", "https://www.mod.gov.lv/"),
 ("lv_vestnesis", "https://www.vestnesis.lv/"),
 ("lv_vugd", "https://www.vugd.gov.lv/"),
 ("lv_mk", "https://www.mk.gov.lv/lv"),
 # Litva
 ("lt_kam", "https://kam.lt/"),
 ("lt_kam_feed", "https://kam.lt/feed/"),
 ("lt_lrv", "https://lrv.lt/lt/"),
 ("lt_pagd", "https://pagd.lrv.lt/"),
 # Finsko
 ("fi_vn", "https://valtioneuvosto.fi/ajankohtaista/tiedotteet"),
 ("fi_pv", "https://puolustusvoimat.fi/"),
 ("fi_112", "https://www.112.fi/"),
 ("fi_fingrid", "https://data.fingrid.fi/api/datasets/124/data?pageSize=2"),
 # Moldavsko
 ("md_gov", "https://gov.md/ro"),
 ("md_gov_rss", "https://gov.md/ro/rss.xml"),
 ("md_igsu", "https://dse.md/"),
 ("md_mai", "https://mai.gov.md/ro"),
 # Rumunsko
 ("ro_mapn", "https://www.mapn.ro/"),
 ("ro_gov", "https://gov.ro/ro"),
 ("ro_igsu", "https://www.igsu.ro/"),
 ("ro_transel", "https://www.transelectrica.ro/sen-filter"),
 # Slovensko
 ("sk_mosr", "https://www.mosr.sk/"),
 ("sk_vlada", "https://www.vlada.gov.sk/"),
 ("sk_minv", "https://www.minv.sk/"),
 ("sk_slovlex", "https://www.slov-lex.sk/"),
 # Maďarsko
 ("hu_kormany", "https://kormany.hu/"),
 ("hu_honved", "https://honvedelem.hu/"),
 ("hu_katved", "https://www.katasztrofavedelem.hu/"),
 ("hu_mk", "https://magyarkozlony.hu/"),
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
    r["delka"] = len(raw); r["zacatek"] = re.sub(r"\s+", " ", t[:700])
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
