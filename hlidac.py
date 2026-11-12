#!/usr/bin/env python3
"""Krizový hlídač – včasné varování před válkou, blackoutem a katastrofami.

Čte veřejné zdroje (NINA/BBK, NOAA, GDACS, Meteoalarm/ČHMÚ, Auswärtiges Amt,
US State Dept, radiační síť BfS a zpravodajské RSS), vyhodnotí stupeň 0–3 pro
domov, práci, Evropu a útočiště, pošle push a obsluhuje webový dashboard
(vlastní server) nebo sestaví šifrovaný statický web (GitHub Pages).

Bez externích závislostí – stačí Python 3.9+.

  python3 hlidac.py               # smyčka každých N minut + dashboard na :8080
  python3 hlidac.py --once        # jeden průchod (např. z cronu)
  python3 hlidac.py --test-notify # zkušební upozornění do všech kanálů
  python3 hlidac.py --demo        # ukázková data pro dashboard, bez sítě
  python3 hlidac.py --site _site  # jeden průchod + statický web (GitHub Actions)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0"
BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DATA = Path(os.environ.get("HLIDAC_DATA", str(BASE / "data")))
CONFIG_PATH = Path(os.environ.get("HLIDAC_CONFIG", str(BASE / "config.json")))
UA = f"KrizovyHlidac/{VERSION} (+https://github.com/; osobni varovny system)"

LEVELS = {0: ("KLID", "🟢"), 1: ("SLEDUJ", "🟡"), 2: ("PŘIPRAV SE", "🟠"), 3: ("ODJEĎ", "🔴")}
HOME_REGIONS = ("CZ", "DE", "EU")  # útočiště se do celkového stupně nepočítají


def refuges(cfg) -> list:
    """Útočiště v pořadí podle preference. Starší konfigurace bez seznamu = jen ES."""
    out = cfg.get("utocista")
    if out:
        return out
    return [{"code": "ES", "name": cfg.get("regions", {}).get("ES", "Španělsko")}] if "ES" in cfg.get("regions", {}) else []


def region_order(cfg) -> list:
    return list(HOME_REGIONS) + [r["code"] for r in refuges(cfg)]


def region_short(code: str) -> str:
    return code if code in HOME_REGIONS else f"Útočiště {code}"

DEFAULT_ACTIONS = {
    "0": ["Zásoby doma průběžně doplňuj.", "Drž nádrž aspoň napůl plnou."],
    "1": ["Zkontroluj zásoby, vodu a léky.", "Natankuj do plna při nejbližší příležitosti.",
          "Sleduj zprávy a dashboard častěji."],
    "2": ["Plná nádrž + kanystr.", "Hotovost v CZK i EUR.", "Doklady všech na jednom místě.",
          "Evakuační tašky u dveří – i věci pro děti.", "Nabít powerbanky, rádio na baterie.",
          "Ověř ubytování v cíli a trasu, vyhni se směru hrozby."],
    "3": ["Odjeď hned – nečekej na oficiální výzvu.", "Trasa pryč od hrozby (u hrozby z východu na západ/jihozápad).",
          "Natankuj, dokud stanice fungují.", "Dej vědět rodině, kde a kdy se sejdete."],
}

_lock = threading.Lock()


# ───────────────────────── pomocné funkce ─────────────────────────

def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime | None) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    m = re.match(r"^(\d{2})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$", s)   # MAAE: „26-11-10 09:00“ (vídeňský čas)
    if m:
        y, mo, d_, h, mi = map(int, m.groups())
        try:
            return datetime(2000 + y, mo, d_, h, mi, tzinfo=timezone(timedelta(hours=1))).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def norm(s: str) -> str:
    """Malá písmena bez diakritiky – pravidla pak fungují pro češtinu, němčinu i španělštinu."""
    s = (s or "").lower().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[‘’‚‛“”„\"'„“”‚‘’«»]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(el, name: str) -> str:
    for c in el:
        if local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:10]


def make_event(source, region, level, title, url="", detail="", eid=None, when=None):
    title = re.sub(r"\s+", " ", (title or "").strip())
    return {
        "id": eid or f"{source}:{short_hash(title + url)}",
        "source": source, "region": region, "level": int(level),
        "title": title, "detail": detail, "url": url, "time": iso(when or utcnow()),
    }


def load_env_file():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


ENC_CONFIG_PATH = BASE / "config.enc.json"
_config_source_logged = None


def load_config() -> dict:
    """Pořadí: proměnná CONFIG_JSON → config.enc.json (dešifrovaný DASH_TOKEN) → config.json."""
    global _config_source_logged
    cfg, source = None, ""
    if os.environ.get("CONFIG_JSON", "").strip():
        cfg, source = json.loads(os.environ["CONFIG_JSON"]), "proměnná CONFIG_JSON"
    elif ENC_CONFIG_PATH.exists() and os.environ.get("DASH_TOKEN") and "HLIDAC_CONFIG" not in os.environ:
        try:
            cfg = decrypt_blob(json.loads(ENC_CONFIG_PATH.read_text(encoding="utf-8")), os.environ["DASH_TOKEN"])
            source = "config.enc.json (šifrovaná osobní konfigurace)"
        except Exception as e:
            log(f"config.enc.json nejde dešifrovat ({type(e).__name__}) – používám {CONFIG_PATH.name}")
    if cfg is None:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg, source = json.load(f), CONFIG_PATH.name
    if source != _config_source_logged:
        log("Konfigurace:", source)
        _config_source_logged = source
    for k, v in (cfg.get("env") or {}).items():   # přístupové údaje kanálů mohou být v šifrované konfiguraci
        if v and not os.environ.get(k):            # prázdný secret v Actions nesmí přebít hodnotu z konfigurace
            os.environ[k] = str(v)
    if os.environ.get("DASHBOARD_URL"):          # adresa i s tokenem patří do .env/secretu, ne do configu
        cfg["dashboard_url"] = os.environ["DASHBOARD_URL"]
    return cfg


def http_fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post(url: str, form: dict | None = None, js: dict | None = None, timeout: int = 20) -> bytes:
    if js is not None:
        body, ctype = json.dumps(js).encode(), "application/json"
    else:
        body, ctype = urllib.parse.urlencode(form or {}).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone", "Europe/Prague"))
    except Exception:
        return timezone(timedelta(hours=1))


# ───────────────────────── zdroje ─────────────────────────
# Každý zdroj vrací (seznam událostí, krátké info pro dashboard).

NINA_SEV = {"Minor": 0, "Moderate": 1, "Severe": 2, "Extreme": 3}
NINA_WAR = re.compile(r"luftangriff|luftalarm|raketen|zivilschutzalarm|radioaktiv|kerntechnisch|atomwaff")


def nina_level(prefix: str, severity: str, title: str) -> int:
    s = NINA_SEV.get(severity or "", 0)
    if prefix in ("mow", "kat", "biw"):          # civilní ochrana
        lvl = {3: 2, 2: 1, 1: 1}.get(s, 0)
    else:                                        # dwd, lhp (povodně), pol …
        lvl = 1 if s == 3 else 0
    if NINA_WAR.search(norm(title)):
        lvl = 3
    return lvl


def src_nina(cfg, fetch):
    events, n_items = [], 0
    for area in cfg["nina"]["areas"]:
        data = json.loads(fetch(f"https://warnung.bund.de/api31/dashboard/{area['ars']}.json"))
        for it in data or []:
            n_items += 1
            iid = it.get("id", "")
            p = ((it.get("payload") or {}).get("data")) or {}
            sev = p.get("severity") or it.get("severity") or ""
            mtype = (p.get("msgType") or it.get("type") or "").lower()
            title = p.get("headline") or (it.get("i18nTitle") or {}).get("de") or iid
            if mtype == "cancel":
                continue
            lvl = nina_level(iid.split(".", 1)[0].lower(), sev, title)
            if lvl <= 0:
                continue
            events.append(make_event("NINA", "DE", lvl, f"{title} ({area['name']})",
                                     url=f"https://warnung.bund.de/meldungen/{urllib.parse.quote(iid)}/",
                                     eid=f"nina:{iid}", when=parse_date(it.get("sent") or p.get("sent"))))
    return events, f"{len(cfg['nina']['areas'])} oblastí, {n_items} hlášení"


def src_nina_national(cfg, fetch):
    data = json.loads(fetch("https://warnung.bund.de/api31/mowas/mapData.json"))
    events = []
    for it in data or []:
        if (it.get("type") or "").lower() == "cancel":
            continue
        title = (it.get("i18nTitle") or {}).get("de") or it.get("id", "")
        sev = it.get("severity", "")
        if NINA_WAR.search(norm(title)):
            lvl = 3
        elif sev == "Extreme":
            lvl = 1
        else:
            continue
        iid = it.get("id", "")
        events.append(make_event("NINA celostátně", "DE", lvl, title,
                                 url=f"https://warnung.bund.de/meldungen/{urllib.parse.quote(iid)}/",
                                 eid=f"nina:{iid}", when=parse_date(it.get("startDate"))))
    return events, f"{len(data or [])} hlášení MoWaS v Německu"


def src_noaa(cfg, fetch):
    data = json.loads(fetch("https://services.swpc.noaa.gov/products/noaa-scales.json"))
    events, now_g, max_fc, fc_day = [], 0, 0, ""
    for k in ("0", "1", "2", "3"):
        e = data.get(k) or {}
        try:
            g = int(((e.get("G") or {}).get("Scale")) or 0)
        except (TypeError, ValueError):
            g = 0
        if k == "0":
            now_g = g
        elif g > max_fc:
            max_fc, fc_day = g, e.get("DateStamp", "")
    for g, label, key in ((now_g, "probíhá", "now"), (max_fc, f"předpověď na {fc_day}", fc_day)):
        lvl = 2 if g >= 5 else 1 if g >= 4 else 0
        if lvl:
            events.append(make_event("NOAA", "EU", lvl, f"Geomagnetická bouře G{g} ({label}) – riziko pro sítě",
                                     url="https://www.swpc.noaa.gov/", eid=f"noaa:G{g}:{key}"))
    return events, f"teď G{now_g}, předpověď max G{max_fc}"


def src_gdacs(cfg, fetch):
    root = ET.fromstring(fetch("https://www.gdacs.org/xml/rss.xml"))
    countries = cfg["gdacs"]["countries"]
    events, n = [], 0
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        n += 1
        if child_text(item, "iscurrent").lower() == "false":
            continue
        alert = child_text(item, "alertlevel")
        codes = set(re.findall(r"[A-Z]{3}", child_text(item, "iso3").upper()))
        for region in {countries[c] for c in codes if c in countries}:
            lvl = ({"Red": 2, "Orange": 1} if region != "EU" else {"Red": 1}).get(alert, 0)
            if lvl:
                guid = child_text(item, "guid") or child_text(item, "link")
                events.append(make_event("GDACS", region, lvl, child_text(item, "title"),
                                         url=child_text(item, "link"), eid=f"gdacs:{guid}:{alert}:{region}",
                                         when=parse_date(child_text(item, "pubDate"))))
    return events, f"{n} událostí ve světě"


def src_meteoalarm(cfg, fetch):
    events, infos, now = [], [], utcnow()
    for f in cfg["meteoalarm"]["feeds"]:
        root = ET.fromstring(fetch(f["url"]))
        region = f["region"]
        area_re = re.compile(f["area_regex"]) if f.get("area_regex") else None
        seen, count = set(), 0
        for entry in root:
            if local(entry.tag) != "entry":
                continue
            area, sev, event = child_text(entry, "areaDesc"), child_text(entry, "severity"), child_text(entry, "event")
            if child_text(entry, "message_type").lower() == "cancel":
                continue
            exp = parse_date(child_text(entry, "expires"))
            if exp and exp < now:
                continue
            if area_re and not area_re.search(norm(area)):
                continue
            count += 1
            lvl = ({"Extreme": 2, "Severe": 1} if region == "CZ" else {"Extreme": 1}).get(sev, 0)
            key = f"{child_text(entry, 'identifier')}|{area}"
            if not lvl or key in seen:
                continue
            seen.add(key)
            events.append(make_event("Meteoalarm", region, lvl, f"{event} – {area}",
                                     url="https://meteoalarm.org/", eid=f"meteo:{short_hash(key)}",
                                     when=parse_date(child_text(entry, "onset"))))
        infos.append(f"{region}: {count} výstrah")
    return events, ", ".join(infos)


def src_auswaertiges_amt(cfg, fetch):
    resp = json.loads(fetch("https://www.auswaertiges-amt.de/opendata/travelwarning")).get("response", {})
    countries = cfg["auswaertiges_amt"]["countries"]
    events = []
    for v in resp.values():
        if not isinstance(v, dict) or v.get("countryCode") not in countries:
            continue
        region = countries[v["countryCode"]]
        home = region != "EU"
        if v.get("warning"):
            lvl, what = (3 if home else 2), "cestovní varování pro celou zemi"
        elif v.get("partialWarning"):
            lvl, what = (2 if home else 1), "částečné cestovní varování"
        elif v.get("situationWarning") or v.get("situationPartWarning"):
            lvl, what = 1, "bezpečnostní upozornění"
        else:
            continue
        flags = "".join("1" if v.get(k) else "0" for k in ("warning", "partialWarning", "situationWarning", "situationPartWarning"))
        events.append(make_event("Auswärtiges Amt", region, lvl, f"{v.get('countryName', v['countryCode'])}: {what}",
                                 url="https://www.auswaertiges-amt.de/de/reiseundsicherheit",
                                 eid=f"aa:{v['countryCode']}:{flags}"))
    return events, f"sledováno {len(countries)} zemí"


def src_us_state(cfg, fetch):
    root = ET.fromstring(fetch("https://travel.state.gov/_res/rss/TAsTWs.xml"))
    name_to_region = {n.lower(): c["region"] for c in cfg["us_state"]["countries"] for n in c["names"]}
    events, found = [], []
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        title = child_text(item, "title")
        m = re.match(r"\s*(.+?)\s+-\s+Level\s+(\d)", title)
        if not m or m.group(1).lower() not in name_to_region:
            continue
        region, n = name_to_region[m.group(1).lower()], int(m.group(2))
        found.append(f"{m.group(1)} L{n}")
        home = region != "EU"
        lvl = {4: 3 if home else 2, 3: 2 if home else 1}.get(n, 0)
        if lvl:
            events.append(make_event("US State Dept", region, lvl, title, url=child_text(item, "link"),
                                     eid=f"us:{m.group(1)}:L{n}"))
    return events, ", ".join(found) or "sledované země nenalezeny"


RADIATION_ADVICE = ("Při úniku radiace se nejdřív ukryj uvnitř (zavři okna, vypni větrání) a řiď se pokyny HZS. "
                    "Jód ber jen na pokyn úřadů. Odjížděj jen na pokyn nebo napříč směrem větru, ne skrz mrak.")


def src_bfs(cfg, fetch, mem):
    """Radiace z německé sítě BfS (~1 700 sond): zóny u hranic, celé Německo a nárůst proti normálu stanice."""
    c = cfg["bfs_radiation"]
    url = ("https://www.imis.bfs.de/ogc/opendata/ows?service=WFS&version=1.1.0&request=GetFeature"
           "&typeName=opendata:odlinfo_odl_1h_latest&outputFormat=application/json")
    data = json.loads(fetch(url))
    zones = c.get("zones") or [{"name": "Hranice", "bbox_lon_lat": c["bbox_lon_lat"], "region": "CZ"}]

    def in_zone(z, lon, lat):
        lon1, lat1, lon2, lat2 = z["bbox_lon_lat"]
        return lon is not None and lat is not None and lon1 <= lon <= lon2 and lat1 <= lat <= lat2

    base = mem.setdefault("baseline", {})
    stations, latest = [], None
    for f in data.get("features", []):
        p = f.get("properties") or {}
        lon, lat = ((f.get("geometry") or {}).get("coordinates") or [None, None])[:2]
        v = p.get("value")
        if p.get("site_status", 1) != 1 or not isinstance(v, (int, float)):
            continue
        sid = str(p.get("kenn") or p.get("id") or f"{lon},{lat}")
        name = p.get("name") or p.get("plz") or sid
        zoned = any(in_zone(z, lon, lat) for z in zones)
        b = base.get(sid) if zoned else None
        if zoned:   # normál stanice: pomalý průměr, zvýšené hodnoty ho neovlivní
            if b is None:
                base[sid] = v
            elif v < b * 1.3:
                base[sid] = round(b * 0.95 + v * 0.05, 4)
        stations.append({"id": sid, "v": v, "name": name, "lon": lon, "lat": lat, "base": b if b is not None else v})
        end = parse_date(p.get("end_measure"))
        if end and (latest is None or end > latest):
            latest = end

    def stats(group):
        vals = sorted(st["v"] for st in group)
        top = max(group, key=lambda st: st["v"])
        return {"n": len(group), "median": round(vals[len(vals) // 2], 3), "max": round(top["v"], 3), "max_name": top["name"]}

    warn, alarm, n_min = c["warn_usvh"], c["alarm_usvh"], c["min_stations"]
    rise_f, rise_min, rise_n = c.get("rise_factor", 2.0), c.get("rise_min_usvh", 0.25), c.get("rise_min_stations", 5)
    events, zone_out = [], []
    for z in zones:
        zs = [st for st in stations if in_zone(z, st["lon"], st["lat"])]
        if not zs:
            zone_out.append({"name": z["name"], "n": 0, "level": 0})
            continue
        ranked = sorted(zs, key=lambda st: -st["v"])
        top3 = ", ".join(f"{st['name']} {st['v']:.2f}" for st in ranked[:3])
        n_alarm = sum(1 for st in zs if st["v"] >= alarm)
        n_warn = sum(1 for st in zs if st["v"] >= warn)
        rising = [st for st in zs if st["v"] >= rise_min and st["v"] >= rise_f * st["base"]]
        region, slug = z.get("region", "CZ"), short_hash(z["name"])
        lvl, title = 0, ""
        if n_alarm >= n_min:
            lvl, title = 3, f"Vysoká radiace – {z['name']}: {n_alarm} stanic ≥ {alarm} µSv/h ({top3})"
        elif n_warn >= n_min:
            lvl, title = 2, f"Zvýšená radiace – {z['name']}: {n_warn} stanic ≥ {warn} µSv/h ({top3})"
        elif len(rising) >= rise_n:
            lvl, title = 1, (f"Neobvyklý nárůst radiace – {z['name']}: {len(rising)} stanic aspoň {rise_f:g}× nad svým normálem "
                             f"({top3}). Často jde jen o silný déšť.")
        if lvl:
            ev = make_event("BfS radiace", region, lvl, title, url="https://odlinfo.bfs.de/", eid=f"bfs:{slug}:{lvl}",
                            detail=RADIATION_ADVICE)
            ev["advice"] = RADIATION_ADVICE
            events.append(ev)
        zone_out.append({"name": z["name"], **stats(zs), "rising": len(rising), "level": lvl})

    nat_warn = sum(1 for st in stations if st["v"] >= warn)
    if nat_warn >= c["national_min_stations"]:
        ev = make_event("BfS radiace", "EU", 2, f"Zvýšená radiace v Německu: {nat_warn} stanic ≥ {warn} µSv/h",
                        url="https://odlinfo.bfs.de/", eid="bfs:national:warn", detail=RADIATION_ADVICE)
        ev["advice"] = RADIATION_ADVICE
        events.append(ev)
    national = {"name": "Celé Německo", **stats(stations), "level": 2 if nat_warn >= c["national_min_stations"] else 0} if stations else {}
    info = "; ".join(f"{z['name']}: max {z.get('max', 0):.2f} µSv/h ({z['n']} stanic)" for z in zone_out)
    return events, f"{info}; v DE {len(stations)} stanic", {"zones": zone_out, "national": national, "time": iso(latest)}


CHMI = "https://opendata.chmi.cz/hydrology/now"
SPA_NAMES = {1: "1. SPA – bdělost", 2: "2. SPA – pohotovost", 3: "3. SPA – ohrožení", 4: "extrémní povodeň"}


def _chmi_stage(value, limits):
    stage = 0
    for i, lim in enumerate(limits, start=1):
        if lim is not None and value is not None and value >= lim:
            stage = i
    return stage


def src_chmi(cfg, fetch, mem):
    """Vodočty ČHMÚ: aktuální stupeň povodňové aktivity, předpověď a rychlé stoupání hladiny."""
    c = cfg["chmi_hydro"]
    now = utcnow()
    meta = mem.get("meta")
    if not meta or now - (parse_date(mem.get("meta_time")) or now - timedelta(days=2)) > timedelta(hours=24):
        raw = json.loads(fetch(f"{CHMI}/metadata/meta1.json"))["data"]["data"]
        header = [h.strip() for h in raw["header"].split(",")]
        wanted = {s["id"] for s in c["stations"]}
        meta = {}
        for row in raw["values"]:
            r = dict(zip(header, row))
            if r.get("objID") in wanted:
                meta[r["objID"]] = {k: r.get(k) for k in ("STATION_NAME", "STREAM_NAME", "SPA1H", "SPA2H", "SPA3H", "SPA4H",
                                                          "SPA1Q", "SPA2Q", "SPA3Q", "SPA4Q")}
        mem["meta"], mem["meta_time"] = meta, iso(now)

    spa_level = {int(k): v for k, v in c.get("levels", {"2": 1, "3": 2, "4": 3}).items()}
    rise_cm, rise_h = c.get("rise_cm", 40), c.get("rise_hours", 3)
    events, rivers = [], []
    for s in c["stations"]:
        m = meta.get(s["id"], {})
        obj = json.loads(fetch(f"{CHMI}/data/{s['id']}.json"))["objList"][0]
        ts = {t["tsConID"]: [(parse_date(d["dt"]), d["value"]) for d in t.get("tsData", []) if d.get("value") is not None]
              for t in obj.get("tsList", [])}
        use_h = any(m.get(f"SPA{i}H") is not None for i in (1, 2, 3))
        key, unit = ("H", "cm") if use_h else ("Q", "m³/s")
        limits = [m.get(f"SPA{i}{key}") for i in (1, 2, 3, 4)]
        series = ts.get(key) or []
        if not series:
            continue
        t_now, v_now = series[-1]
        stage = _chmi_stage(v_now, limits)
        fc = [(t, v) for t, v in ts.get(f"{key}_F", []) if t and t > now and t - now <= timedelta(hours=48)]
        fc_max = max(fc, key=lambda x: x[1]) if fc else None
        fc_stage = _chmi_stage(fc_max[1], limits) if fc_max else 0
        h = ts.get("H") or []
        past = [v for t, v in h if t and t_now and timedelta(0) < t_now - t <= timedelta(hours=rise_h)]
        rise = (h[-1][1] - min(past)) if (h and past) else 0
        name = s.get("name") or f"{m.get('STATION_NAME')} – {m.get('STREAM_NAME')}"
        url = "https://hydro.chmi.cz/hppsoldv/hpps_main.php"
        lvl_now, lvl_fc = spa_level.get(stage, 0), spa_level.get(fc_stage, 0)
        if lvl_now:
            events.append(make_event("ČHMÚ vodočty", s.get("region", "CZ"), lvl_now,
                                     f"{name}: {SPA_NAMES[stage]} ({v_now} {unit})", url=url,
                                     eid=f"chmi:{s['id']}:now:{stage}", when=t_now))
        if lvl_fc > lvl_now:
            events.append(make_event("ČHMÚ vodočty", s.get("region", "CZ"), lvl_fc,
                                     f"{name}: předpověď {SPA_NAMES[fc_stage]} ({fc_max[1]} {unit}, "
                                     f"{fc_max[0].astimezone(local_tz(cfg)).strftime('%d.%m. %H:%M')})", url=url,
                                     eid=f"chmi:{s['id']}:fc:{fc_stage}", when=now))
        if rise >= rise_cm and not lvl_now and not lvl_fc:
            events.append(make_event("ČHMÚ vodočty", s.get("region", "CZ"), 1,
                                     f"{name}: hladina stoupla o {rise:.0f} cm za {rise_h} h (teď {h[-1][1]} cm)", url=url,
                                     eid=f"chmi:{s['id']}:rise", when=t_now))
        rivers.append({"name": name, "value": v_now, "unit": unit, "time": iso(t_now), "stage": stage,
                       "limits": limits[:3], "forecast_max": fc_max[1] if fc_max else None, "forecast_stage": fc_stage,
                       "rise": round(rise) if h else None})
    info = ", ".join(f"{r['name'].split(' –')[0]} {r['value']} {r['unit']}" for r in rivers)
    return events, info or "žádná data", {"rivers": rivers}


AUTOBAHN = "https://verkehr.autobahn.de/o/autobahn"


def route_roads(cfg) -> list:
    roads = []
    for t in cfg.get("plan", {}).get("trasy", []):
        for r in t.get("dalnice", []):
            if r not in roads:
                roads.append(r)
    return roads


def src_route(cfg, fetch):
    """Uzavírky a kolony na německých dálnicích tvých tras (Autobahn GmbH)."""
    c = cfg.get("route_status", {})
    min_delay = c.get("min_delay_minutes", 30)
    roads = route_roads(cfg)

    def one(road):
        out = {"closures": [], "jams": []}
        for item in json.loads(fetch(f"{AUTOBAHN}/{road}/services/closure", timeout=15)).get("closure", []):
            if item.get("future") is True:
                continue
            out["closures"].append({"title": item.get("title", ""), "dir": (item.get("subtitle") or "").strip(),
                                    "blocked": str(item.get("isBlocked")).lower() == "true"})
        for item in json.loads(fetch(f"{AUTOBAHN}/{road}/services/warning", timeout=15)).get("warning", []):
            try:
                delay = int(float(item.get("delayTimeValue") or 0))
            except (TypeError, ValueError):
                delay = 0
            if delay >= min_delay or str(item.get("isBlocked")).lower() == "true":
                out["jams"].append({"title": item.get("title", ""), "dir": (item.get("subtitle") or "").strip(),
                                    "delay": delay, "blocked": str(item.get("isBlocked")).lower() == "true"})
        out["jams"].sort(key=lambda j: -j["delay"])
        out["problem"] = any(x["blocked"] for x in out["closures"] + out["jams"]) or any(j["delay"] >= 60 for j in out["jams"])
        return road, out

    result, errors = {}, []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for fut in [ex.submit(one, r) for r in roads]:
            try:
                road, out = fut.result()
                result[road] = out
            except Exception as e:
                errors.append(f"{type(e).__name__}")
    if roads and not result:
        raise RuntimeError("žádná dálnice neodpověděla: " + ", ".join(errors))
    bad = [r for r, o in result.items() if o["problem"]]
    info = f"{len(result)} dálnic, komplikace: {', '.join(bad) or 'žádné'}"
    return [], info, {"roads": result, "time": iso(utcnow())}


def src_fuel(cfg, fetch, mem):
    """Palivo v Německu (Tankerkönig, zdarma s klíčem). Každý běh jeden bod trasy – limit API je 1 dotaz/min."""
    c = cfg["fuel"]
    key = os.environ.get("TANKERKOENIG_KEY", "")
    if not key:
        return [], "vypnuto – chybí klíč TANKERKOENIG_KEY", {"points": {}}
    points = c["points"]
    i = mem.get("i", 0) % len(points)
    mem["i"] = i + 1
    p = points[i]
    url = (f"https://creativecommons.tankerkoenig.de/json/list.php?lat={p['lat']}&lng={p['lon']}"
           f"&rad={c.get('radius_km', 10)}&type=all&apikey={key}")
    data = json.loads(fetch(url))
    if not data.get("ok"):
        raise RuntimeError(f"Tankerkönig: {data.get('message', 'chyba')}")
    st = data.get("stations", [])
    open_st = [s for s in st if s.get("isOpen")]
    prices = [s["diesel"] for s in open_st if isinstance(s.get("diesel"), (int, float)) and s["diesel"] > 0]
    avg = round(sum(prices) / len(prices), 3) if prices else None
    pts = mem.setdefault("points", {})
    pm = pts.setdefault(p["name"], {"hist": []})
    if avg:
        pm["hist"] = (pm["hist"] + [avg])[-400:]
    hist = sorted(pm["hist"][:-1])
    base = hist[len(hist) // 2] if len(hist) >= 20 else None
    pm.update(time=iso(utcnow()), open=len(open_st), total=len(st), diesel=avg, baseline=base)
    events = []
    hour = utcnow().astimezone(local_tz(cfg)).hour
    for name, m in pts.items():
        if m.get("total", 0) >= 5 and 7 <= hour <= 20 and m["open"] / m["total"] < c.get("min_open_share", 0.4):
            events.append(make_event("Palivo", "DE", 1, f"Zavřené pumpy u {name}: otevřeno {m['open']} z {m['total']}",
                                     url="https://creativecommons.tankerkoenig.de/", eid=f"fuel:{name}:closed"))
        if m.get("baseline") and m.get("diesel") and m["diesel"] >= m["baseline"] * c.get("price_jump", 1.25):
            pct = round((m["diesel"] / m["baseline"] - 1) * 100)
            events.append(make_event("Palivo", "DE", 1, f"Nafta u {name} dražší o {pct} % ({m['diesel']:.2f} €)",
                                     url="https://creativecommons.tankerkoenig.de/", eid=f"fuel:{name}:price"))
    summary = {n: {k: m.get(k) for k in ("time", "open", "total", "diesel", "baseline")} for n, m in pts.items()}
    return events, f"{p['name']}: otevřeno {len(open_st)}/{len(st)}, nafta {avg or '?'} €", {"points": summary}


SOURCES = {
    "nina":           ("NINA – místa staveb a hranice", src_nina, "nina"),
    "nina_national":  ("NINA – celé Německo", src_nina_national, "nina_national"),
    "noaa":           ("NOAA – kosmické počasí", src_noaa, "noaa"),
    "gdacs":          ("GDACS – katastrofy", src_gdacs, "gdacs"),
    "meteoalarm":     ("Meteoalarm / ČHMÚ / AEMET", src_meteoalarm, "meteoalarm"),
    "aa":             ("Auswärtiges Amt", src_auswaertiges_amt, "auswaertiges_amt"),
    "us_state":       ("US State Dept", src_us_state, "us_state"),
    "bfs":            ("BfS – radiace", src_bfs, "bfs_radiation"),
    "chmi":           ("ČHMÚ – vodočty", src_chmi, "chmi_hydro"),
    "route":          ("Dálnice na trase (DE)", src_route, "route_status"),
    "fuel":           ("Palivo na trase (DE)", src_fuel, "fuel"),
}


# ───────────────────────── zprávy (RSS) ─────────────────────────

def parse_feed(raw: bytes):
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = child_text(el, "title")
        link = child_text(el, "link")
        if not link:
            for c in el:
                if local(c.tag) == "link" and c.get("href"):
                    link = c.get("href")
                    break
        date = parse_date(child_text(el, "pubDate") or child_text(el, "published")
                          or child_text(el, "updated") or child_text(el, "date"))
        if title:
            out.append((html.unescape(title), link, date))
    return out


def compile_rules(cfg):
    rules = []
    for r in cfg["news"]["rules"]:
        rules.append({**r, "_m": re.compile(r["match"]), "_x": re.compile(r["exclude"]) if r.get("exclude") else None})
    return rules


def match_rules(title: str, rules) -> list:
    t = norm(title)
    return [r for r in rules if r["_m"].search(t) and not (r["_x"] and r["_x"].search(t))]


def run_news(cfg, fetch, state, now):
    """Vrátí (události, zdravotní stav feedů). Zásahy drží `hold_hours`, potvrzení = ≥2 různé zdroje."""
    nc = cfg["news"]
    rules = compile_rules(cfg)
    hits = state.setdefault("news_hits", {})
    seen = state.setdefault("news_seen", {})
    health = {}
    max_age = timedelta(hours=nc.get("max_age_hours", 24))

    def one(feed):
        return feed, parse_feed(fetch(feed["url"]))

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, f) for f in nc["feeds"]]
        for fut, feed in zip(futs, nc["feeds"]):
            try:
                _, items = fut.result()
                health[feed["id"]] = {"ok": True, "info": f"{len(items)} titulků"}
                for title, link, date in items:
                    if date and now - date > max_age:
                        continue
                    for r in match_rules(title, rules):
                        if r.get("feeds") and feed["id"] not in r["feeds"]:   # pravidlo jen pro určité kanály
                            continue
                        key = f"{r['id']}|{feed['id']}|{short_hash(norm(title))}"
                        if key not in hits and key not in seen:   # vypršelý titulek se znovu nepočítá
                            hits[key] = {"rule": r["id"], "feed": feed["id"], "feed_name": feed["name"],
                                         "title": title, "url": link, "seen": iso(now)}
            except Exception as e:
                health[feed["id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}

    hold = timedelta(hours=nc.get("hold_hours", 12))
    for k in [k for k, h in hits.items() if now - (parse_date(h["seen"]) or now) > hold]:
        seen[k] = hits.pop(k)["seen"]
    for k in [k for k, t in seen.items() if now - (parse_date(t) or now) > timedelta(hours=72)]:
        del seen[k]

    events = []
    for r in rules:
        rh = sorted((h for h in hits.values() if h["rule"] == r["id"]), key=lambda h: h["seen"], reverse=True)
        if not rh:
            continue
        feeds = sorted({h["feed_name"] for h in rh})
        confirmed = len(feeds) >= 2
        lvl = r["confirmed"] if confirmed else r["single"]
        tag = "potvrzeno" if confirmed else "jen 1 zdroj – ověř"
        detail = f"{len(feeds)} zdroj(e): {', '.join(feeds)}"
        if r.get("note"):
            detail += f". {r['note']}"
        ev = make_event("Zprávy", r["region"], lvl, f"{r['label']} ({tag}): {rh[0]['title']}",
                        url=rh[0]["url"], detail=detail, eid=f"news:{r['id']}:L{lvl}", when=parse_date(rh[0]["seen"]))
        if r.get("advice"):
            ev["advice"] = r["advice"]
        events.append(ev)
    return events, health


# ───────────────────────── notifikace ─────────────────────────

def channels_configured():
    out = []
    if os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER"):
        out.append("pushover")
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if os.environ.get("NTFY_TOPIC"):
        out.append("ntfy")
    return out


def send_notification(title: str, message: str, level: int, url: str = "") -> list:
    sent = []
    if os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER"):
        pr = {0: -1, 1: 0, 2: 1, 3: 2}[level]
        form = {"token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
                "title": title[:250], "message": message[:1024], "priority": pr}
        if url:
            form.update(url=url[:512], url_title="Otevřít")
        if pr == 2:
            form.update(retry=60, expire=3600, sound="siren")
        try:
            http_post("https://api.pushover.net/1/messages.json", form=form)
            sent.append("pushover")
        except Exception as e:
            log("Pushover selhal:", e)
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}"
        if url:
            text += f'\n<a href="{html.escape(url)}">Otevřít</a>'
        try:
            http_post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
                      js={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000], "parse_mode": "HTML",
                          "disable_web_page_preview": True, "disable_notification": level == 0})
            sent.append("telegram")
        except Exception as e:
            log("Telegram selhal:", e)
    if os.environ.get("NTFY_TOPIC"):
        server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")   # prázdný secret = výchozí server
        body = {"topic": os.environ["NTFY_TOPIC"], "title": title, "message": message,
                "priority": {0: 2, 1: 3, 2: 4, 3: 5}[level], "tags": [["white_check_mark", "warning", "rotating_light", "rotating_light"][level]]}
        if url:
            # ntfy.sh je veřejný server – kód k webu do odkazu nedávej, prohlížeč si ho pamatuje
            body["click"] = re.sub(r"[?&]k=[^&#]*", "", url)
        try:
            http_post(server + "/", js=body)
            sent.append("ntfy")
        except Exception as e:
            log("ntfy selhal:", e)
    if not channels_configured():
        log("[bez kanálu] ", title, "|", message.replace("\n", " / "))
    return sent


def recommend_refuge(cfg, region_levels):
    """Nejklidnější útočiště; při shodě rozhoduje pořadí v konfiguraci (preference)."""
    refs = refuges(cfg)
    if not refs:
        return None
    _, best = min(enumerate(refs), key=lambda x: (region_levels.get(x[1]["code"], 0), x[0]))
    lvl = region_levels.get(best["code"], 0)
    return {"code": best["code"], "name": best.get("name", best["code"]), "level": lvl, "label": LEVELS[lvl][0],
            "emoji": LEVELS[lvl][1], "trasa": best.get("trasa"), "pozn": best.get("pozn", ""), "calm": lvl <= 1}


def route_issues(cfg, route_extra, trasa_id=None, limit=4) -> list:
    """Krátké řádky o komplikacích na dálnicích dané trasy (nebo všech tras)."""
    roads = (route_extra or {}).get("roads", {})
    trasy = cfg.get("plan", {}).get("trasy", [])
    names = []
    for t in trasy:
        if trasa_id is None or t.get("id") == trasa_id:
            names += [d for d in t.get("dalnice", []) if d not in names]
    out = []
    for d in names:
        r = roads.get(d) or {}
        for c in r.get("closures", []):
            if c.get("blocked"):
                out.append(f"{c['title']} uzavřeno ({c['dir']})".replace(" ()", ""))
        for j in r.get("jams", []):
            out.append(f"{j['title']}: zdržení {j['delay']} min ({j['dir']})".replace(" ()", ""))
    return out[:limit]


def alert_context(cfg, state, region_levels):
    rec = recommend_refuge(cfg, region_levels)
    route = state.get("extras", {}).get("route")
    return {"recommended": rec, "route_issues": route_issues(cfg, route, rec.get("trasa") if rec else None)}


def context_lines(ctx) -> list:
    lines = []
    rec = (ctx or {}).get("recommended")
    if rec:
        if rec["calm"]:
            lines.append(f"Doporučený cíl: {rec['name']} ({rec['emoji']} {rec['label'].lower()})")
        else:
            lines.append(f"Pozor: žádné útočiště není klidné, nejlepší je {rec['name']} ({rec['emoji']} {rec['label'].lower()})")
    issues = (ctx or {}).get("route_issues") or []
    if issues:
        lines.append("Na trase: " + "; ".join(issues))
    return lines


def compose_alert(cfg, new_events, overall, ctx=None):
    top = max(e["level"] for e in new_events)
    name, emoji = LEVELS[top]
    regions = []
    for e in sorted(new_events, key=lambda e: -e["level"]):
        if e["level"] == top and region_short(e["region"]) not in regions:
            regions.append(region_short(e["region"]))
    title = f"{emoji} {name}: {', '.join(regions)}"
    lines = []
    for e in sorted(new_events, key=lambda e: -e["level"])[:6]:
        lines.append(f"{LEVELS[e['level']][1]} [{region_short(e['region'])}] {e['title']} ({e['source']})")
    if len(new_events) > 6:
        lines.append(f"… a další ({len(new_events) - 6})")
    on, oe = LEVELS[overall]
    lines.append("")
    lines.append(f"Celkový stav doma: {oe} {on}")
    advice = []
    for e in new_events:
        if e.get("advice") and e["advice"] not in advice:
            advice.append(e["advice"])
    acts = (cfg.get("actions") or DEFAULT_ACTIONS).get(str(top), [])
    if advice:
        lines += [f"⚠️ {a}" for a in advice]
    elif top >= 1 and acts:
        lines.append("Co teď: " + " ".join(acts[:3]))
    if top >= 2:
        lines += context_lines(ctx)
    url = cfg.get("dashboard_url") or new_events[0].get("url", "")
    return title, "\n".join(lines), top, url


def record_history(state, title, message, level, sent):
    h = state.setdefault("history", [])
    h.append({"time": iso(utcnow()), "title": title, "message": message, "level": level, "channels": sent})
    del h[:-50]


def notify(state, cfg, title, message, level, url=""):
    sent = send_notification(title, message, level, url)
    record_history(state, title, message, level, sent)
    log(f"Upozornění L{level}: {title} → {', '.join(sent) or 'žádný kanál'}")


# ───────────────────────── hlavní cyklus ─────────────────────────

def load_state() -> dict:
    p = DATA / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log("state.json je poškozený – začínám znovu")
    return {}


def atomic_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_cycle(cfg, state, fetch=http_fetch, do_notify=True, now=None):
    now = now or utcnow()
    nc = cfg.get("notify", {})
    src_state = state.setdefault("sources", {})
    last_events = state.setdefault("last_events", {})
    stale_limit = timedelta(hours=nc.get("source_fail_hours", 6))
    all_events = []

    enabled = {k: v for k, v in SOURCES.items() if cfg.get(v[2], {}).get("enabled", False)}
    src_mem = state.setdefault("source_memory", {})     # vlastní paměť zdroje (rotace, průměry)
    extras = state.setdefault("extras", {})             # strukturovaná data pro dashboard (vodočty, trasa)

    def call(key, fn):
        if fn.__code__.co_argcount >= 3:
            return fn(cfg, fetch, src_mem.setdefault(key, {}))
        return fn(cfg, fetch)

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(call, key, fn): key for key, (_, fn, _) in enabled.items()}
        results = {}
        for fut in cf.as_completed(futs):
            key = futs[fut]
            try:
                results[key] = ("ok", fut.result())
            except Exception as e:
                results[key] = ("err", f"{type(e).__name__}: {e}"[:200])

    for key, (label, _, _) in enabled.items():
        st = src_state.setdefault(key, {})
        st["label"] = label
        kind, val = results[key]
        if kind == "ok":
            events, info = val[0], val[1]
            if len(val) > 2:
                extras[key] = val[2]
            st.update(ok=True, last_ok=iso(now), info=info, error="")
            st.pop("fail_since", None)
            st.pop("fail_notified", None)
            last_events[key] = events
            all_events += events
        else:
            st.update(ok=False, error=val)
            st.setdefault("fail_since", iso(now))
            last_ok = parse_date(st.get("last_ok"))
            if last_ok and now - last_ok < stale_limit:   # krátký výpadek zdroje: drž poslední známý stav
                all_events += last_events.get(key, [])

    if cfg.get("news", {}).get("enabled"):
        news_events, health = run_news(cfg, fetch, state, now)
        all_events += news_events
        ok_feeds = [k for k, h in health.items() if h["ok"]]
        st = src_state.setdefault("news", {})
        st["label"] = "Zprávy (RSS)"
        st["feeds"] = health
        if ok_feeds:
            st.update(ok=True, last_ok=iso(now), info=f"{len(ok_feeds)}/{len(health)} zdrojů funguje", error="")
            st.pop("fail_since", None)
            st.pop("fail_notified", None)
        else:
            st.update(ok=False, error="žádný zpravodajský zdroj nefunguje")
            st.setdefault("fail_since", iso(now))
        bad = [k for k, h in health.items() if not h["ok"]]
        if bad and ok_feeds:
            st["info"] += f" (nefunguje: {', '.join(bad)})"

    for key in list(src_state):     # vypnuté zdroje z přehledu vyřaď
        if key != "news" and key not in enabled:
            src_state.pop(key)
            extras.pop(key, None)

    # sloučit duplicity (stejné id) – nechat nejvyšší stupeň
    merged = {}
    for e in all_events:
        if e["id"] not in merged or e["level"] > merged[e["id"]]["level"]:
            merged[e["id"]] = e
    events = sorted(merged.values(), key=lambda e: (-e["level"], e["region"], e["title"]))

    region_levels = {r: max([e["level"] for e in events if e["region"] == r] or [0]) for r in region_order(cfg)}
    overall = max(region_levels[r] for r in HOME_REGIONS)

    if do_notify:
        handle_notifications(cfg, state, events, overall, region_levels, now)

    status = build_status(cfg, state, events, region_levels, overall, now)
    atomic_write(DATA / "status.json", status)
    atomic_write(DATA / "state.json", state)
    ping_healthcheck(src_state)
    return status


def handle_notifications(cfg, state, events, overall, region_levels, now):
    nc = cfg.get("notify", {})
    min_level = nc.get("min_level", 1)
    renotify = timedelta(hours=nc.get("renotify_hours", 6))
    notified = state.setdefault("notified", {})
    active = {e["id"]: e for e in events if e["level"] >= min_level}
    prev_active = set(state.get("prev_active", []))
    prev_overall = state.get("prev_overall", 0)

    ctx = alert_context(cfg, state, region_levels)
    url_dash = cfg.get("dashboard_url", "")
    sent_l3 = False

    if not state.get("initialized"):
        on, oe = LEVELS[overall]
        lines = [f"Celkový stav doma: {oe} {on}"]
        for r in region_order(cfg):
            n, e = LEVELS[region_levels[r]]
            lines.append(f"{e} {cfg['regions'].get(r, r)}: {n}")
        bad = [s.get("label", k) for k, s in state.get("sources", {}).items() if not s.get("ok")]
        if bad:
            lines.append("⚠️ Nefunguje: " + ", ".join(bad))
        if overall >= 2:
            lines += context_lines(ctx)
        notify(state, cfg, "▶️ Krizový hlídač spuštěn", "\n".join(lines), overall, url_dash)
        sent_l3 = overall >= 3
        for i in active:
            notified[i] = iso(now)
        state["initialized"] = True
    else:
        new = []
        for i, e in active.items():
            last = parse_date(notified.get(i))
            if i not in prev_active and not (last and now - last < renotify):
                new.append(e)
        if new:
            title, msg, lvl, url = compose_alert(cfg, new, overall, ctx)
            notify(state, cfg, title, msg, lvl, url)
            sent_l3 = lvl >= 3
            for e in new:
                notified[e["id"]] = iso(now)
        elif overall < prev_overall:
            on, oe = LEVELS[overall]
            notify(state, cfg, f"⬇️ Zlepšení: {oe} {on}",
                   f"Celkový stav doma klesl z {LEVELS[prev_overall][0]} na {on}.", 0, url_dash)

    # stupeň Odjeď trvá – připomínat, dokud nepomine (ntfy na iPhonu neprobudí, opakování ano)
    repeat = nc.get("repeat_level3_minutes", 30)
    if sent_l3:
        state["last_l3"] = iso(now)
    elif overall >= 3 and repeat:
        last = parse_date(state.get("last_l3"))
        if not last or now - last >= timedelta(minutes=repeat) - timedelta(seconds=30):
            top = [e for e in events if e["level"] >= 3 and e["region"] in HOME_REGIONS][:3]
            lines = [f"🔴 [{region_short(e['region'])}] {e['title']}" for e in top]
            lines += [""] + context_lines(ctx)
            advice = next((e["advice"] for e in top if e.get("advice")), None)
            acts = (cfg.get("actions") or DEFAULT_ACTIONS).get("3", [])
            if advice:
                lines.append(f"⚠️ {advice}")
            elif acts:
                lines.append("Co teď: " + " ".join(acts[:2]))
            notify(state, cfg, "🔴 Stále ODJEĎ", "\n".join(lines).strip(), 3, url_dash)
            state["last_l3"] = iso(now)

    # nefunkční zdroje – jedno upozornění
    fail_limit = timedelta(hours=nc.get("source_fail_hours", 6))
    for key, st in state.get("sources", {}).items():
        since = parse_date(st.get("fail_since"))
        if not st.get("ok") and since and now - since >= fail_limit and not st.get("fail_notified"):
            notify(state, cfg, f"⚠️ Zdroj nefunguje: {st.get('label', key)}",
                   f"Od {since.astimezone(local_tz(cfg)).strftime('%d.%m. %H:%M')} – {st.get('error', '')}\n"
                   "Hlídání je v této oblasti slepé. Zkontroluj adresu v config.json.", 0)
            st["fail_notified"] = True

    # týdenní „jsem naživu“
    hb = nc.get("weekly_heartbeat")
    if hb:
        lt = now.astimezone(local_tz(cfg))
        week = lt.strftime("%G-%V")
        if lt.weekday() == hb.get("weekday", 6) and lt.hour == hb.get("hour", 18) and state.get("heartbeat_week") != week:
            on, oe = LEVELS[overall]
            ok = sum(1 for s in state.get("sources", {}).values() if s.get("ok"))
            notify(state, cfg, "✅ Hlídač běží", f"Stav doma: {oe} {on}. Funkčních zdrojů: {ok}/{len(state.get('sources', {}))}.", 0,
                   cfg.get("dashboard_url", ""))
            state["heartbeat_week"] = week

    for i in [i for i, t in notified.items() if now - (parse_date(t) or now) > timedelta(days=14)]:
        del notified[i]
    state["prev_active"] = list(active)
    state["prev_overall"] = overall


def build_status(cfg, state, events, region_levels, overall, now):
    def lv(n):
        return {"level": n, "label": LEVELS[n][0], "emoji": LEVELS[n][1]}
    refs = {r["code"]: r for r in refuges(cfg)}
    rec = recommend_refuge(cfg, region_levels)
    regions = []
    for r in region_order(cfg):
        name = refs[r].get("name") if r in refs else None
        regions.append({"code": r, "name": name or cfg["regions"].get(r, r), "refuge": r not in HOME_REGIONS,
                        "recommended": bool(rec and rec["code"] == r), "note": refs.get(r, {}).get("pozn", ""),
                        **lv(region_levels.get(r, 0)), "events": [e for e in events if e["region"] == r][:5]})
    extras = state.get("extras", {})
    route = extras.get("route") or {}
    trasy = []
    for t in cfg.get("plan", {}).get("trasy", []):
        roads = [{"road": d, **(route.get("roads", {}).get(d) or {"closures": [], "jams": [], "problem": None})}
                 for d in t.get("dalnice", [])]
        trasy.append({**t, "roads": roads, "issues": route_issues(cfg, route, t.get("id"), limit=8)})
    sources = []
    for key, st in state.get("sources", {}).items():
        sources.append({"key": key, "label": st.get("label", key), "ok": st.get("ok", False),
                        "last_ok": st.get("last_ok", ""), "info": st.get("info", ""), "error": st.get("error", ""),
                        "feeds": st.get("feeds")})
    ok = sum(1 for s in sources if s["ok"])
    interval = cfg.get("interval_minutes", 10)
    # GitHub Actions spouští plán s občasným zpožděním – varování „stará data“ až po delší době
    stale = cfg.get("stale_after_minutes") or (75 if os.environ.get("GITHUB_ACTIONS") == "true" else max(30, 3 * interval))
    return {
        "version": VERSION, "generated": iso(now), "interval_minutes": interval, "stale_after_minutes": stale,
        "blind": bool(sources) and ok * 2 < len(sources),   # většina zdrojů mlčí → „Klid“ by lhal
        "overall": lv(overall), "regions": regions, "events": events, "sources": sources,
        "actions": cfg.get("actions") or DEFAULT_ACTIONS, "plan": {**cfg.get("plan", {}), "trasy": trasy},
        "recommended": rec, "route_time": route.get("time", ""),
        "rivers": (extras.get("chmi") or {}).get("rivers", []),
        "radiation": extras.get("bfs") or {},
        "fuel": (extras.get("fuel") or {}).get("points", {}),
        "history": list(reversed(state.get("history", [])))[:20],
        "channels": channels_configured(),
    }


def ping_healthcheck(src_state):
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    if src_state and not any(s.get("ok") for s in src_state.values()):
        url = url.rstrip("/") + "/fail"
    try:
        http_fetch(url, timeout=10)
    except Exception as e:
        log("Healthcheck ping selhal:", e)


# ───────────────────────── web server ─────────────────────────

def make_handler(token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KrizovyHlidac"

        def log_message(self, *a):
            pass

        def _authorized(self, query) -> bool:
            if not token:
                return True
            tok = token.encode()
            if secrets.compare_digest(query.get("k", [""])[0].encode(), tok):
                return True
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hk" and secrets.compare_digest(v.encode(), tok):
                    return True
            return False

        def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path or "/"
            if path == "/health":
                st = DATA / "status.json"
                age = int(time.time() - st.stat().st_mtime) if st.exists() else -1
                return self._send(200, f"ok {age}".encode())
            if not self._authorized(q):
                return self._send(401, "Přístup zamítnut. Otevři odkaz s ?k=TVŮJ_TOKEN.".encode())
            extra = {}
            if token and q.get("k"):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                extra["Set-Cookie"] = f"hk={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax{secure}"
            if path == "/status.json":
                p = DATA / "status.json"
                body = p.read_bytes() if p.exists() else b'{"error":"zatim zadna data"}'
                extra["Cache-Control"] = "no-store"
                return self._send(200, body, "application/json; charset=utf-8", extra)
            if path == "/manifest.webmanifest":
                m = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
                if token:
                    m["start_url"] = f"./?k={token}"
                return self._send(200, json.dumps(m, ensure_ascii=False).encode(), "application/manifest+json", extra)
            if path == "/":
                path = "/index.html"
            f = (WEB / path.lstrip("/")).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"404")
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            if f.name in ("sw.js", "index.html"):
                extra["Cache-Control"] = "no-cache"
            return self._send(200, f.read_bytes(), ctype, extra)

    return Handler


def serve(cfg):
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    token = os.environ.get("DASH_TOKEN", "")
    h = cfg.get("http", {})
    srv = ThreadingHTTPServer((h.get("bind", "0.0.0.0"), int(os.environ.get("PORT", h.get("port", 8080)))), make_handler(token))
    log(f"Dashboard běží na http://{srv.server_address[0]}:{srv.server_address[1]}/" + (" (chráněno tokenem)" if token else " (BEZ ochrany – nastav DASH_TOKEN)"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ───────────────────────── statický web (GitHub Pages) ─────────────────────────

ENC_ITER = 200_000


def encrypt_status(obj: dict, token: str) -> dict:
    """AES-256-GCM s klíčem z PBKDF2-SHA256(token). Prohlížeč dešifruje přes WebCrypto."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # pip install cryptography
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, ENC_ITER, dklen=32)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ENC_ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt_blob(env: dict, token: str):
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = base64.b64decode
    key = hashlib.pbkdf2_hmac("sha256", token.encode(), d(env["salt"]), env["iter"], dklen=32)
    return json.loads(AESGCM(key).decrypt(d(env["iv"]), d(env["ct"]), None))


def build_site(site_dir: Path, status: dict):
    """Zkopíruje web a přidá stav. S DASH_TOKEN jen šifrovaně – web na Pages je veřejný."""
    import shutil
    site_dir = Path(site_dir)
    if site_dir.exists():
        shutil.rmtree(site_dir)
    shutil.copytree(WEB, site_dir)
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    token = os.environ.get("DASH_TOKEN", "")
    if token:
        atomic_write(site_dir / "status.enc.json", encrypt_status(status, token))
    else:
        log("POZOR: DASH_TOKEN chybí – stav se zveřejní nešifrovaně")
        atomic_write(site_dir / "status.json", status)
    log("Web sestaven do", site_dir)


# ───────────────────────── demo ─────────────────────────

def demo_status(cfg):
    now = utcnow()
    state = {"initialized": True, "sources": {
        k: {"label": v[0], "ok": True, "last_ok": iso(now), "info": "ukázka"} for k, v in SOURCES.items()}}
    state["sources"]["news"] = {"label": "Zprávy (RSS)", "ok": True, "last_ok": iso(now), "info": "6/7 zdrojů funguje (nefunguje: mdr)"}
    state["sources"]["us_state"].update(ok=False, error="TimeoutError: timed out")
    state["history"] = [{"time": iso(now - timedelta(minutes=35)), "title": "🟠 PŘIPRAV SE: EU",
                         "message": "🟠 [EU] Útok na území NATO (potvrzeno) …", "level": 2, "channels": ["pushover"]}]
    events = [
        make_event("Zprávy", "EU", 2, "Útok na území NATO (potvrzeno): Ruské drony zasáhly sklad u Řešova, Polsko žádá konzultace",
                   url="https://example.org", detail="3 zdroj(e): BBC World, ČT24, iROZHLAS", eid="demo1", when=now - timedelta(minutes=35)),
        make_event("NOAA", "EU", 1, "Geomagnetická bouře G4 (předpověď na 2026-11-14) – riziko pro sítě",
                   url="https://www.swpc.noaa.gov/", eid="demo2"),
        make_event("Meteoalarm", "CZ", 1, "Severe Impact of Snow/Ice – Hlavní město Praha",
                   url="https://meteoalarm.org/", eid="demo3"),
        make_event("NINA", "DE", 1, "Gefahreninformation: Großbrand, Rauchentwicklung (Dresden)",
                   url="https://warnung.bund.de/", eid="demo4"),
    ]
    events.append(make_event("Zprávy", "ES", 2, "Krize ve Španělsku (útočiště) (potvrzeno): Gran apagón en toda España",
                             url="https://example.org", eid="demo5"))
    state["extras"] = {
        "chmi": {"rivers": [
            {"name": "Louny – Ohře", "value": 182, "unit": "cm", "time": iso(now), "stage": 0, "limits": [400, 440, 490],
             "forecast_max": 190, "forecast_stage": 0, "rise": 12}]},
        "route": {"time": iso(now), "roads": {
            "A6": {"closures": [{"title": "A6 | Schwetzingen/Hockenheim - Walldorf", "dir": "Mannheim -> Heilbronn", "blocked": True}],
                   "jams": [], "problem": True},
            "A5": {"closures": [], "jams": [], "problem": False},
            "A3": {"closures": [], "jams": [{"title": "A3 | Würzburg - Wertheim", "dir": "Frankfurt -> Nürnberg", "delay": 45,
                                             "blocked": False}], "problem": False},
            "A4": {"closures": [], "jams": [], "problem": False}}},
        "fuel": {"points": {"Norimberk": {"time": iso(now), "open": 23, "total": 25, "diesel": 1.629, "baseline": 1.61}}},
        "bfs": {"time": iso(now - timedelta(minutes=40)), "zones": [
            {"name": "Hranice ČR – Sasko a Bavorsko", "n": 64, "median": 0.098, "max": 0.148, "max_name": "Erlbach", "rising": 0, "level": 0},
            {"name": "Šumava – směr Temelín", "n": 21, "median": 0.112, "max": 0.161, "max_name": "Bayerisch Eisenstein", "rising": 0, "level": 0}],
            "national": {"name": "Celé Německo", "n": 1688, "median": 0.081, "max": 0.192, "max_name": "Feldberg", "level": 0}},
    }
    rl = {r: max([e["level"] for e in events if e["region"] == r] or [0]) for r in region_order(cfg)}
    overall = max(rl[r] for r in HOME_REGIONS)
    return build_status(cfg, state, events, rl, overall, now)


# ───────────────────────── vstup ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Krizový hlídač")
    ap.add_argument("--once", action="store_true", help="jeden průchod a konec")
    ap.add_argument("--no-serve", action="store_true", help="bez webového dashboardu")
    ap.add_argument("--test-notify", action="store_true", help="pošli zkušební upozornění")
    ap.add_argument("--demo", action="store_true", help="ukázková data do dashboardu (bez sítě)")
    ap.add_argument("--site", metavar="SLOŽKA", help="po průchodu sestav statický web (GitHub Pages); implikuje --once")
    ap.add_argument("--encrypt-config", metavar="SOUBOR", help="zašifruj osobní konfiguraci do config.enc.json (klíč = DASH_TOKEN)")
    ap.add_argument("--decrypt-config", action="store_true", help="vypiš dešifrovanou config.enc.json (pro úpravy)")
    args = ap.parse_args()
    if args.site:
        args.once = True

    load_env_file()
    if args.encrypt_config or args.decrypt_config:
        token = os.environ.get("DASH_TOKEN")
        if not token:
            sys.exit("Nastav DASH_TOKEN (v .env nebo proměnné prostředí).")
        if args.decrypt_config:
            print(json.dumps(decrypt_blob(json.loads(ENC_CONFIG_PATH.read_text(encoding="utf-8")), token), ensure_ascii=False, indent=2))
        else:
            cfg = json.loads(Path(args.encrypt_config).read_text(encoding="utf-8"))
            atomic_write(ENC_CONFIG_PATH, encrypt_status(cfg, token))
            print(f"Zašifrováno do {ENC_CONFIG_PATH.name} – můžeš ho nahrát do repa, bez DASH_TOKEN je nečitelný.")
        return
    cfg = load_config()
    DATA.mkdir(parents=True, exist_ok=True)

    if args.test_notify:
        ch = channels_configured()
        if not ch:
            print("Není nastaven žádný kanál – vyplň .env (PUSHOVER_*, TELEGRAM_* nebo NTFY_TOPIC).")
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print("::error title=Zkušební upozornění::Není nastaven žádný kanál (secrets PUSHOVER_TOKEN a PUSHOVER_USER, TELEGRAM_* nebo NTFY_TOPIC).", flush=True)
            sys.exit(1)
        for lvl in (1, 3):
            sent = send_notification(f"{LEVELS[lvl][1]} Test hlídače – stupeň {lvl}",
                                     "Toto je zkouška. Pokud tě stupeň 3 probudil i v režimu Nerušit, je vše nastavené správně.",
                                     lvl, cfg.get("dashboard_url", ""))
            print(f"Stupeň {lvl}: odesláno přes {', '.join(sent) or 'nic (chyba)'}")
            if os.environ.get("GITHUB_ACTIONS") == "true":
                kind = "notice" if sent else "error"
                print(f"::{kind} title=Zkušební upozornění::Stupeň {lvl}: {', '.join(sent) or 'neodesláno – zkontroluj přístupové údaje kanálu'}", flush=True)
        return

    if args.demo:
        st = demo_status(cfg)
        atomic_write(DATA / "status.json", st)
        log("Ukázková data zapsána do", DATA / "status.json")
        if args.site:
            build_site(Path(args.site), st)
        if args.once or args.no_serve:
            return
        serve(cfg)
        while True:
            time.sleep(3600)

    if not args.once and not args.no_serve:
        serve(cfg)

    log(f"Krizový hlídač {VERSION} – kanály: {', '.join(channels_configured()) or 'žádné (jen dashboard)'}")
    while True:
        started = time.time()
        try:
            cfg = load_config()   # změny v config.json se projeví bez restartu
            with _lock:
                state = load_state()
                st = run_cycle(cfg, state)
            log(f"Průchod hotov: celkem {st['overall']['emoji']} {st['overall']['label']}, "
                f"událostí {len(st['events'])}, nefunkční zdroje: "
                f"{', '.join(s['key'] for s in st['sources'] if not s['ok']) or 'žádné'}")
            if os.environ.get("GITHUB_ACTIONS") == "true":   # přehled v záložce Actions – bez osobních údajů
                ok = sum(1 for s in st["sources"] if s["ok"])
                print(f"::notice title=Zdroje {ok}/{len(st['sources'])}::" + ", ".join(
                    f"{s['key']} {'OK' if s['ok'] else 'CHYBA'}" for s in st["sources"]), flush=True)
                for s in st["sources"]:
                    bad = {k: f["error"] for k, f in (s.get("feeds") or {}).items() if not f["ok"]}
                    if not s["ok"] or bad:
                        detail = s["error"] if not s["ok"] else "; ".join(f"{k}: {e}" for k, e in bad.items())
                        print(f"::warning title=Zdroj {s['key']}::{detail[:400]}", flush=True)
        except Exception:
            log("Chyba průchodu:\n" + traceback.format_exc())
            if args.site:
                sys.exit(1)
        if args.site:
            build_site(Path(args.site), st)
        if args.once:
            return
        wait = max(30, cfg.get("interval_minutes", 10) * 60 - (time.time() - started))
        time.sleep(wait)


if __name__ == "__main__":
    main()
