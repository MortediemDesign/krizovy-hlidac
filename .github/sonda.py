"""Dočasná sonda: státní zdroje východního křídla. Smaže se po ověření."""
import json, re, sys, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
d = now.strftime("%Y-%m-%d")
d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
URLS = [
 ("hu_kv_hirek", "https://www.katasztrofavedelem.hu/10417/RSS_hirek"),
 ("hu_kv_kozl", "https://www.katasztrofavedelem.hu/10463/RSS_kozlemenyek"),
 ("hu_kv_sug", "https://www.katasztrofavedelem.hu/10464/RSS_hattersugarzas"),
 ("hu_mk_feed", "https://magyarkozlony.hu/feed"),
 ("md_gov_rss", "https://gov.md/ro/rss.xml"),
 ("lv_ast_rss", "https://www.ast.lv/lv/rss.xml"),
 ("ee_riigikogu", "https://www.riigikogu.ee/en/feed/"),
 ("lt_lrt_en", "https://www.lrt.lt/en/news-in-english?rss"),
 ("lt_lrt_rss2", "https://www.lrt.lt/rss"),
 ("lt_seimas_rss", "https://www.lrs.lt/sip/portal.show?p_r=35403&p_k=1&rss=1"),
 ("pl_pr24", "https://www.polskieradio24.pl/rss"),
 ("pl_pap", "https://www.pap.pl/rss.xml"),
 ("pl_pse_day", "https://api.raporty.pse.pl/api/kse-load?$filter=business_date%20eq%20%272026-11-15%27&$first=200"),
 ("ee_elering_plan", "https://dashboard.elering.ee/api/system/with-plan?start=2026-11-15T00:00:00.000Z&end=2026-11-15T23:59:59.999Z"),
 ("fi_yle_en", "https://yle.fi/rss/news"),
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
    r["polozky"] = [re.sub(r"\s+", " ", x)[:900] for x in re.findall(r"<item[ >].*?</item>|<entry[ >].*?</entry>", t, re.S)[:2]]
    try:
        js = json.loads(t)
        def shape(o, dp=0):
            if dp > 4: return "…"
            if isinstance(o, dict): return {k: shape(v, dp + 1) for k, v in list(o.items())[:30]}
            if isinstance(o, list): return [f"len={len(o)}", shape(o[0], dp + 1), shape(o[1], dp + 1) if len(o) > 1 else None, shape(o[-1], dp + 1)] if o else []
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
