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
import io
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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
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
    """Malá písmena bez diakritiky – pravidla pak fungují pro češtinu, němčinu, španělštinu i jazyky východního křídla."""
    s = (s or "").lower().replace("ß", "ss").replace("ł", "l").replace("đ", "d").replace("ø", "o")
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


def http_fetch(url: str, timeout: int = 25, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
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
    return events, f"teď G{now_g}, předpověď max G{max_fc}", {"now": now_g, "max": max_fc, "day": fc_day}


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
    events, flagged = [], {}
    for v in resp.values():
        if isinstance(v, dict) and v.get("countryCode"):
            f = {k: bool(v.get(k)) for k in ("warning", "partialWarning", "situationWarning", "situationPartWarning")}
            if any(f.values()):            # pro mapu Evropy: všechny země s varováním
                flagged[v["countryCode"]] = {"name": v.get("countryName", ""), **f}
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
    return events, f"sledováno {len(countries)} zemí, s varováním celkem {len(flagged)}", {"zeme": flagged}


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
    return events, f"{info}; v DE {len(stations)} stanic", {"zones": zone_out, "national": national, "time": iso(latest),
                                                            "warn": warn, "alarm": alarm}


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


# ───────────────────── energie, internet, úřady, zemětřesení ─────────────────────

def _num(x):
    try:
        return float(str(x).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _due(mem, key, hours, now) -> bool:
    """Je čas znovu stáhnout pomalu se měnící data? Šetří cizí servery i čas běhu."""
    t = parse_date(mem.get(key))
    return not t or now - t >= timedelta(hours=hours) - timedelta(minutes=2)


AGSI = "https://agsi.gie.eu/api"
GAS_ADVICE = ("Málo plynu v zásobnících znamená riziko omezení dodávek a tlak na elektrickou síť. "
              "Měj zásoby na 72 h, záložní teplo a sleduj vyhlášení stupňů nouze.")


def src_gas(cfg, fetch, mem):
    """Zásobníky plynu (GIE AGSI+, klíč zdarma po registraci). Data jsou denní – stahují se jednou za pár hodin."""
    c = cfg["gas"]
    key = os.environ.get("AGSI_KEY", "")
    if not key:
        return [], "vypnuto – chybí klíč AGSI_KEY (zdarma po registraci na agsi.gie.eu)", {"zeme": []}
    now = utcnow()
    note = ""
    if _due(mem, "time", c.get("refresh_hours", 6), now) or not mem.get("zeme"):
        def refresh():
            out = []
            for z in c["zeme"]:
                code = z["code"].upper()
                q = "type=eu" if code == "EU" else f"country={code.lower()}"
                rows = _gie_rows(fetch(f"{AGSI}?{q}&size=10", headers={"x-key": key}))
                rows = [r for r in rows if _num(r.get("full")) is not None]
                if not rows:
                    raise RuntimeError(f"AGSI {code}: odpověď bez dat")
                r = max(rows, key=lambda r: str(r.get("gasDayStart", "")))
                twh = _num(r.get("gasInStorage"))
                net = (_num(r.get("withdrawal")) or 0) - (_num(r.get("injection")) or 0)   # GWh/den, + = odběr
                out.append({"code": code, "name": z.get("name", code),
                            "region": z.get("region", code if code in ("CZ", "DE") else "EU"),
                            "day": r.get("gasDayStart", ""), "full": round(_num(r.get("full")), 1), "twh": twh,
                            "net_gwh": round(net), "trend": _num(r.get("trend")),
                            "days": round(twh * 1000 / net) if twh and net > 50 else None})
            mem["zeme"], mem["time"] = out, iso(now)
        note = _keep_on_failure(mem, now, 12, refresh)
    events = []
    for g in mem["zeme"]:
        days = g.get("days")
        lvl = 0
        if g["full"] < c.get("pripravit_pod_pct", 10) or (days and days < c.get("pripravit_dni", 14)):
            lvl = 2
        elif g["full"] < c.get("sledovat_pod_pct", 20) or (days and days < c.get("sledovat_dni", 30)):
            lvl = 1
        g["level"] = lvl
        if lvl:
            dny = f", při dnešním odběru by vystačily na {days} dní" if days else ""
            ev = make_event("Plyn (AGSI)", g["region"], lvl, f"Zásobníky plynu {g['name']}: {g['full']:.0f} %{dny}",
                            url="https://agsi.gie.eu/", eid=f"gas:{g['code']}:{lvl}", detail=GAS_ADVICE)
            events.append(ev)
    info = ", ".join(f"{g['code']} {g['full']:.0f} %" for g in mem["zeme"]) + note
    return events, info, {"zeme": mem["zeme"], "time": mem.get("time", "")}


ALSI = "https://alsi.gie.eu/api"
IIP = "https://iip.gie.eu/api"
GIE_UNITS = {"gwh/d": 1, "mwh/d": 1e-3, "kwh/d": 1e-6, "gwh/h": 24, "mwh/h": 24e-3, "kwh/h": 24e-6}


class GieTemporary(RuntimeError):
    """Server GIE vrátil místo dat svou vnitřní chybovou odpověď – obvykle přechodné přetížení."""


def _gie_rows(raw, meta: dict | None = None) -> list:
    """Řádky z odpovědi GIE (AGSI/ALSI/IIP). Jiný formát = chyba, ne tiché „nic se neděje“."""
    j = json.loads(raw)
    if isinstance(j, dict) and "statusCode" in j and "content" in j and "data" not in j:
        # GIE občas vrátí serializovaný objekt odpovědi svého serveru (statusCode, content…) místo dat
        inner = j.get("content")
        try:
            inner = json.loads(inner) if isinstance(inner, str) else inner
        except ValueError:
            inner = None
        if isinstance(inner, list) or (isinstance(inner, dict) and "data" in inner):
            j = inner
        else:
            raise GieTemporary(f"GIE dočasně neposlal data (kód {j.get('statusCode')})")
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(f"GIE: {j['error']}"[:150])
    if meta is not None and isinstance(j, dict):
        meta.update({k: j.get(k) for k in ("last_page", "total")})
    rows = j.get("data", j) if isinstance(j, dict) else j
    if not isinstance(rows, list):
        raise RuntimeError("GIE: neznámý formát odpovědi " + (str(list(j)[:6]) if isinstance(j, dict) else type(j).__name__))
    return [r for r in rows if isinstance(r, dict)]


def _keep_on_failure(mem, now, hours, fn):
    """Obnov data; když server krátce nedodá, nech poslední známá data (max. `hours` hodin staré)."""
    try:
        fn()
        mem.pop("fail_since", None)
        return ""
    except Exception as e:
        last = parse_date(mem.get("time"))
        since = parse_date(mem.get("fail_since")) or now
        mem["fail_since"] = iso(since)
        if last and now - last <= timedelta(hours=hours) and now - since <= timedelta(hours=hours):
            return f" (poslední data z {last:%d. %m. %H:%M} UTC, GIE teď neodpovídá)"
        raise e


def _gwh(x):
    return _num(x.get("gwh")) if isinstance(x, dict) else _num(x)


def _gwh_day(cap, unit):
    v, u = _num(cap), re.sub(r"\s+", "", str(unit or "")).lower()
    if v is None:
        return None
    return next((v * f for k, f in GIE_UNITS.items() if u.startswith(k)), None)


def _running(start, end, now) -> bool:
    s = parse_date(str(start)) if start else None
    e = parse_date(str(end)) if end else None
    return (s is None or s <= now) and (e is None or e >= now)


def _name(x):
    return (x.get("name") or "") if isinstance(x, dict) else str(x or "")


LNG_ADVICE = ("Prudký pokles dovozu zkapalněného plynu. Může jít o počasí nebo zpoždění lodí, "
              "ale i o problém s dodávkami – sleduj zprávy a zásobníky.")


def src_lng(cfg, fetch, mem):
    """Dodávky z LNG terminálů (GIE ALSI). Hlídá prudký pokles proti obvyklým dnům."""
    c = cfg["lng"]
    key = os.environ.get("AGSI_KEY", "")
    if not key:
        return [], "vypnuto – chybí klíč AGSI_KEY", {"zeme": []}
    now = utcnow()
    note = ""
    if _due(mem, "time", c.get("refresh_hours", 6), now) or not mem.get("zeme"):
        def refresh():
            out = []
            for z in c["zeme"]:
                rows = _gie_rows(fetch(f"{ALSI}?country={z['code'].lower()}&size=30", headers={"x-key": key}))
                rows = sorted((r for r in rows if _num(r.get("sendOut")) is not None and r.get("gasDayStart")),
                              key=lambda r: str(r["gasDayStart"]), reverse=True)
                if not rows:
                    raise RuntimeError(f"ALSI {z['code']}: odpověď bez dat")
                cur = rows[0]
                hist = sorted(_num(r["sendOut"]) for r in rows[1:15])
                inv, cap = _gwh(cur.get("inventory")), _gwh(cur.get("dtmi"))
                out.append({"code": z["code"].upper(), "name": z.get("name", z["code"]), "region": z.get("region", "EU"),
                            "day": cur["gasDayStart"], "send": round(_num(cur["sendOut"])),
                            "base": round(hist[len(hist) // 2]) if hist else None,
                            "fill": round(inv / cap * 100) if inv is not None and cap else None,
                            "cap": _num(cur.get("dtrs"))})
            mem["zeme"], mem["time"] = out, iso(now)
        note = _keep_on_failure(mem, now, 12, refresh)
    events = []
    for g in mem["zeme"]:
        base = g.get("base")
        g["level"] = 1 if (base and base >= c.get("min_normal_gwh", 50)
                           and g["send"] < base * c.get("pokles_pod", 0.3)) else 0
        if g["level"]:
            events.append(make_event("LNG (ALSI)", g["region"], 1,
                                     f"Dovoz LNG – {g['name']}: {g['send']} GWh/d, obvykle {base} GWh/d",
                                     url="https://alsi.gie.eu/", eid=f"lng:{g['code']}", detail=LNG_ADVICE))
    info = ", ".join(f"{g['code']} {g['send']} GWh/d (obvykle {g.get('base')})" for g in mem["zeme"]) + note
    return events, info, {"zeme": mem["zeme"], "time": mem.get("time", "")}


OUTAGE_NOTE = "Neplánovaný výpadek snižuje kapacitu – nemusí hned znamenat méně plynu, ale zhoršuje rezervu."


def src_gas_outages(cfg, fetch, mem):
    """Neplánované výpadky plynové infrastruktury (GIE): zásobníky (AGSI), LNG terminály (ALSI)
    a plynovody (IIP, urgentní zprávy provozovatelů přepravních soustav)."""
    c = cfg["plyn_vypadky"]
    key = os.environ.get("AGSI_KEY", "")
    if not key:
        return [], "vypnuto – chybí klíč AGSI_KEY", {"oblasti": [], "items": []}
    now = utcnow()
    h = {"x-key": key}
    parts, fails = mem.setdefault("parts", {}), mem.setdefault("fails", {})
    if _due(mem, "time", c.get("refresh_minutes", 30) / 60, now) or "items" not in mem:
        def run(key, fn):
            try:
                parts[key] = fn()
                fails.pop(key, None)
            except Exception as e:      # jedna část (třeba IIP) krátce neodpoví → zůstanou poslední známá data
                since = parse_date(fails.get(key)) or now
                fails[key] = iso(since)
                if now - since > timedelta(hours=c.get("chyba_po_hodinach", 3)):
                    raise RuntimeError(f"{key}: {e}"[:200])
                parts.setdefault(key, [])

        def agsi(code):
            return [{"code": code, "kind": "zásobník", "name": _name(r.get("facility")) or "zásobník",
                     "gwh": _num(r.get("withdrawal")) or _num(r.get("injection")), "to": r.get("end"),
                     "pozn": str(r.get("description") or "")[:160]}
                    for r in _gie_rows(fetch(f"{AGSI}/unavailability?country={code}&type=Unplanned&size=100", headers=h))
                    if _running(r.get("start"), r.get("end"), now)]

        def alsi(code):
            return [{"code": code, "kind": "LNG terminál", "name": _name(r.get("facility")) or "LNG terminál",
                     "gwh": _num(r.get("capacity")), "to": r.get("end"), "pozn": str(r.get("description") or "")[:160]}
                    for r in _gie_rows(fetch(f"{ALSI}/unavailability?country={code}&type=Unplanned&size=100", headers=h))
                    if _running(r.get("start"), r.get("end"), now)]

        # plynovody: IIP nemá filtr země – provozovatele přepravní soustavy poznáme podle názvu
        tso = [(re.compile(z["provozovatele"], re.I), z["code"].upper()) for z in c["zeme"] if z.get("provozovatele")]

        def iip():
            out = []
            for page in range(1, c.get("iip_pages", 3) + 1):
                meta = {}
                rows = _gie_rows(fetch(f"{IIP}?status=active&reportingEntityType=TSO&size=300&page={page}", headers=h), meta)
                for r in rows:
                    msg = r.get("message") or {}
                    if "unplanned" not in str(msg.get("unavailabilityType", "")).lower():
                        continue
                    if not _running(r.get("from"), r.get("to"), now):
                        continue
                    ent, asset = r.get("reportingEntity") or {}, r.get("asset") or {}
                    text = f"{_name(ent)} {_name(asset)} {r.get('balancingZone') or ''}"
                    code = next((cd for rx, cd in tso if rx.search(text)), None)
                    if not code:
                        continue
                    un = r.get("unavailable") or {}
                    out.append({"code": code, "kind": "plynovod", "name": _name(asset) or _name(ent),
                                "gwh": _gwh_day(un.get("capacity"), un.get("unit")) if isinstance(un, dict) else None,
                                "to": r.get("to"), "pozn": str(r.get("unavailabilityReason") or r.get("remarks") or "")[:160]})
                last_page = _num(meta.get("last_page"))
                if len(rows) < 300 or (last_page is not None and page >= last_page):
                    break
            return out

        for z in c["zeme"]:
            code = z["code"].upper()
            run(f"AGSI {code}", lambda code=code: agsi(code))
            if z.get("lng"):
                run(f"ALSI {code}", lambda code=code: alsi(code))
        if tso:
            run("IIP", iip)
        items = [dict(i) for v in parts.values() for i in v]
        for it in items:
            if it["gwh"] is not None:
                it["gwh"] = round(it["gwh"])
        mem["items"], mem["time"] = items, iso(now)
    items = mem["items"]
    regions, events = [], []
    for z in c["zeme"]:
        code = z["code"].upper()
        mine = [i for i in items if i["code"] == code]
        total = sum(i["gwh"] or 0 for i in mine)
        lvl = 2 if total >= z.get("pripravit_gwh", 1000) else 1 if total >= z.get("sledovat_gwh", 300) else 0
        regions.append({"code": code, "name": z.get("name", code), "gwh": total, "n": len(mine), "level": lvl})
        if lvl:
            top = sorted(mine, key=lambda i: -(i["gwh"] or 0))[:2]
            events.append(make_event("Plyn – výpadky (GIE)", z.get("region", code), lvl,
                                     f"Neplánované výpadky plynu – {z.get('name', code)}: {total} GWh/d ({len(mine)} hlášení)",
                                     url="https://iip.gie.eu/", eid=f"gasout:{code}:{lvl}",
                                     detail="Největší: " + "; ".join(f"{i['name']} ({i['kind']}, {i['gwh']} GWh/d)" for i in top)
                                            + ". " + OUTAGE_NOTE))
    info = ", ".join(f"{r['code']} {r['gwh']} GWh/d ({r['n']})" for r in regions)
    if fails:
        info += f"; dočasně bez odpovědi: {', '.join(sorted(fails))} (drží se poslední data)"
    top = sorted(items, key=lambda i: -(i["gwh"] or 0))[:12]
    return events, info, {"oblasti": regions, "items": top, "time": mem.get("time", "")}


IODA = "https://api.ioda.inetintel.cc.gatech.edu/v2"
IODA_NOISY = {"merit-nt", "ucsd-nt"}   # teleskopy – samy o sobě často falešný poplach
IODA_NAMES = {"bgp": "směrování BGP", "ping-slash24": "aktivní měření", "merit-nt": "teleskop", "ucsd-nt": "teleskop",
              "gtr": "provoz Google", "gtr-norm": "provoz Google"}
INTERNET_ADVICE = ("Celostátní výpadek internetu bývá první známkou blackoutu nebo kyberútoku. "
                   "Ověř to rádiem, nabij powerbanky a natankuj.")


def src_ioda(cfg, fetch):
    """Výpadky internetu celých zemí (IODA, Georgia Tech). Kritický pokles ve ≥2 měřeních = rozsáhlý výpadek."""
    c = cfg["internet"]
    now = utcnow()
    until = int(now.timestamp())
    frm = until - int(c.get("okno_hodin", 6) * 3600)
    fresh = c.get("cerstve_hodin", 2) * 3600

    def one(z):
        url = f"{IODA}/outages/alerts?entityType=country&entityCode={z['code']}&from={frm}&until={until}&limit=500"
        raw = json.loads(fetch(url, timeout=20))
        if isinstance(raw, dict) and raw.get("error"):
            raise RuntimeError(f"IODA: {raw['error']}"[:150])
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict):
            data = data.get("alerts", data.get("data"))
        if not isinstance(data, list):     # jiný formát = hlásit chybu, ne tiše „bez výpadku“
            keys = list(raw)[:6] if isinstance(raw, dict) else type(raw).__name__
            raise RuntimeError(f"IODA: neznámý formát odpovědi {keys}")
        latest = {}
        for a in data or []:
            if not isinstance(a, dict):
                continue
            ds, t = a.get("datasource"), a.get("time")
            if ds and isinstance(t, (int, float)) and (ds not in latest or t > latest[ds][0]):
                latest[ds] = (t, str(a.get("level", "")).lower())
        down = sorted(ds for ds, (t, lv) in latest.items() if lv == "critical" and until - t <= fresh)
        return z, down

    out, events, errors = [], [], []
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        for fut in [ex.submit(one, z) for z in c["zeme"]]:
            try:
                z, down = fut.result()
            except Exception as e:
                errors.append(type(e).__name__)
                continue
            core = [d for d in down if d not in IODA_NOISY]
            lvl = 2 if (len(down) >= 2 and core) else 1 if core else 0
            name = z.get("name", z["code"])
            if lvl:
                srcs = ", ".join(IODA_NAMES.get(d, d) for d in down)
                tail = "" if lvl == 2 else " Jedno měření může být chyba – ověř."
                ev = make_event("IODA internet", z.get("region", z["code"]), lvl,
                                f"Výpadek internetu – {name}: kritický pokles ({srcs}).{tail}",
                                url=f"https://ioda.inetintel.cc.gatech.edu/country/{z['code']}",
                                eid=f"ioda:{z['code']}:{lvl}", detail=INTERNET_ADVICE)
                if lvl >= 2:
                    ev["advice"] = INTERNET_ADVICE
                events.append(ev)
            out.append({"code": z["code"], "name": name, "down": down, "level": lvl})
    if c["zeme"] and not out:
        raise RuntimeError("IODA neodpověděla: " + ", ".join(errors))
    bad = [o["code"] for o in out if o["level"]]
    return events, f"{len(out)} zemí, výpadek: {', '.join(bad) or 'nikde'}", {"zeme": out, "time": iso(now)}


GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_points(raw: bytes) -> list:
    try:
        j = json.loads(raw)
    except ValueError:
        raise RuntimeError("GDELT: " + raw[:120].decode("utf-8", "ignore").strip())
    pts = []
    for series in j.get("timeline") or []:
        for d in series.get("data") or []:
            t, v = d.get("date"), d.get("value")
            try:
                dt = datetime.strptime(str(t), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                dt = parse_date(t)
            if dt and isinstance(v, (int, float)):
                pts.append((dt, float(v)))
        break   # první řada = objem zpráv
    return sorted(pts)


def gdelt_spike(pts, now, window_h=6):
    """(teď, normál) – průměr posledních hodin proti mediánu denních průměrů předchozích dní."""
    recent = [v for t, v in pts if now - t <= timedelta(hours=window_h)]
    if not recent and pts and now - pts[-1][0] <= timedelta(hours=30):   # hrubší rozlišení: poslední bod
        recent = [pts[-1][1]]
    days = {}
    for t, v in pts:
        if now - t > timedelta(hours=24):
            days.setdefault(t.date(), []).append(v)
    means = sorted(sum(v) / len(v) for v in days.values())
    if not recent or len(means) < 3:
        return None
    return sum(recent) / len(recent), means[len(means) // 2]


def src_gdelt(cfg, fetch, mem):
    """Prudký nárůst světových zpráv na téma (GDELT, 65 jazyků). Každý běh jeden dotaz – GDELT omezuje četnost."""
    c = cfg["gdelt"]
    now = utcnow()
    queries = c["dotazy"]
    res = mem.setdefault("q", {})
    i = mem.get("i", 0) % len(queries)
    mem["i"] = i + 1
    q = queries[i]
    url = (f"{GDELT}?query={urllib.parse.quote(q['dotaz'])}&mode=timelinevol&timespan={c.get('timespan', '7d')}"
           f"&format=json")
    pts = None
    for attempt in range(2):   # GitHub sdílí IP adresy s dalšími uživateli GDELT – při omezení jednou zkus znovu
        try:
            pts = gdelt_points(fetch(url, timeout=30))
            break
        except Exception as e:
            limited = getattr(e, "code", None) == 429 or "limit requests" in str(e).lower()
            if not limited:
                raise
            if attempt == 0:
                time.sleep(c.get("retry_seconds", 6))
    limited = pts is None
    if limited:                # krátké omezení přečkej s posledními výsledky, dlouhé nahlas ohlas
        since = parse_date(mem.get("last_ok"))
        if since is None:
            mem["last_ok"] = iso(now)
        elif now - since > timedelta(hours=c.get("limit_hours", 12)):
            raise RuntimeError(f"GDELT omezuje dotazy už přes {c.get('limit_hours', 12)} h")
        pts = []
    else:
        mem["last_ok"] = iso(now)
    sp = gdelt_spike(pts, now, c.get("okno_hodin", 6))
    if sp:
        cur, base = sp
        ratio = cur / max(base, c.get("min_normal", 0.0005))
        res[q["id"]] = {"name": q["nazev"], "cur": round(cur, 5), "base": round(base, 5), "ratio": round(ratio, 1),
                        "time": iso(now)}
    events = []
    for qq in queries:
        r = res.get(qq["id"])
        if not r or now - (parse_date(r["time"]) or now) > timedelta(hours=3):
            continue
        if r["ratio"] >= qq.get("pomer", c.get("pomer", 4)) and r["cur"] >= qq.get("min", c.get("min", 0.02)):
            events.append(make_event("GDELT svět", qq.get("region", "EU"), 1,
                                     f"Svět o tom najednou píše víc: {qq['nazev']} ({r['ratio']:.0f}× víc zpráv než obvykle)",
                                     url=f"https://api.gdeltproject.org/api/v2/doc/doc?query={urllib.parse.quote(qq['dotaz'])}&mode=artlist&timespan=24h",
                                     eid=f"gdelt:{qq['id']}",
                                     detail="Samo o sobě to neznamená nebezpečí – podívej se do zpráv, co se děje."))
    info = ", ".join(f"{r['name']} {r['ratio']}×" for r in res.values()) or "zatím bez dat"
    if limited:
        info = "GDELT teď omezuje dotazy, zkusím příště; " + info
    return events, info, {"dotazy": [{"id": qq["id"], **res[qq["id"]]} for qq in queries if qq["id"] in res]}


NUKIB = "https://nukib.gov.cz/cs/infoservis/aktuality/"


def src_nukib(cfg, fetch, mem):
    """Úřední varování NÚKIB. Varuje zřídka, takže nové varování před útoky je signál."""
    c = cfg["nukib"]
    now = utcnow()
    page = fetch(c.get("url", NUKIB)).decode("utf-8", "ignore")
    items = {}
    for m in re.finditer(r'<a[^>]+href="([^"]*/infoservis/aktuality/(\d+)-[^"#?]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        href, iid, inner = m.groups()
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        if len(title) >= 8 and iid not in items:
            items[iid] = (title, urllib.parse.urljoin("https://nukib.gov.cz/", href))
    if not items:
        raise RuntimeError("na stránce nejsou aktuality – změnil se web?")
    match = re.compile(c.get("match", r"varovan"))
    threat = re.compile(c.get("hrozba", r"utok|hrozb[a-z]* utok|konflikt|valk|rusk|kriticke infrastruktur|energet|nemocnic|sabotaz"))
    active = mem.setdefault("active", {})
    if mem.get("seen") is None:          # první běh = výchozí stav, staré zprávy nehlásit
        mem["seen"] = sorted(items, key=int)[-300:]
    else:
        seen = set(mem["seen"])
        for iid, (title, url) in items.items():
            if iid in seen:
                continue
            seen.add(iid)
            t = norm(title)
            if match.search(t) and threat.search(t):
                active[iid] = {"title": title, "url": url, "first": iso(now)}
        mem["seen"] = sorted(seen, key=int)[-300:]
    hold = timedelta(days=c.get("drzet_dni", 7))
    for k in [k for k, a in active.items() if now - (parse_date(a["first"]) or now) > hold]:
        del active[k]
    events = [make_event("NÚKIB", "CZ", 1, f"Varování NÚKIB: {a['title']}", url=a["url"], eid=f"nukib:{k}",
                         when=parse_date(a["first"]),
                         detail="Úřední kybernetické varování. Měj zálohy a hotovost, výpadky služeb jsou možné.")
              for k, a in active.items()]
    return events, f"{len(items)} aktualit, aktivní varování: {len(active)}"


PSP_TISKY = "https://www.psp.cz/eknih/cdrom/opendata/tisky.zip"
PSP_RULES = [
    {"match": r"(vyhlas|vyhlaseni)[a-z ]{0,25}(stavu ohrozeni statu|valecneho stavu)", "level": 2},
    {"match": r"stav[a-z]* ohrozeni statu|valecn[a-z]* stav|mobilizac|brann[a-z]* pohotovost", "level": 1},
]


def src_psp(cfg, fetch, mem):
    """Nové sněmovní tisky (otevřená data PSP, denně). Vládní návrh na stav ohrožení státu se tu objeví dřív než ve zprávách."""
    c = cfg["psp"]
    now = utcnow()
    rules = [(re.compile(r["match"]), r["level"]) for r in c.get("pravidla", PSP_RULES)]
    if _due(mem, "time", c.get("refresh_hours", 6), now):
        zf = zipfile.ZipFile(io.BytesIO(fetch(c.get("url", PSP_TISKY), timeout=60)))
        name = next((n for n in zf.namelist() if n.lower().rsplit("/", 1)[-1] == "tisky.unl"), None)
        if not name:
            raise RuntimeError("v tisky.zip chybí tisky.unl")
        rows = {}
        for line in zf.read(name).decode("cp1250", "replace").splitlines():
            f = line.split("|")
            if len(f) < 5 or not f[0].strip().isdigit():
                continue
            rows[int(f[0])] = max((x.strip() for x in f[1:]), key=len)   # nejdelší pole = úplný název tisku
        if not rows:
            raise RuntimeError("tisky.unl je prázdný")
        top = max(rows)
        if mem.get("max_id") is not None:
            hits = mem.setdefault("hits", {})
            for tid in sorted(t for t in rows if t > mem["max_id"]):
                t = norm(rows[tid])
                lvl = next((lv for rx, lv in rules if rx.search(t)), 0)
                if lvl:
                    hits[str(tid)] = {"title": rows[tid][:300], "level": lvl, "seen": iso(now)}
        mem["max_id"] = max(top, mem.get("max_id") or 0)
        mem["time"], mem["count"] = iso(now), len(rows)
    hits = mem.setdefault("hits", {})
    hold = timedelta(days=c.get("drzet_dni", 14))
    for k in [k for k, h in hits.items() if now - (parse_date(h["seen"]) or now) > hold]:
        del hits[k]
    events = [make_event("Sněmovna", "CZ", h["level"], f"Nový sněmovní tisk: {h['title']}",
                         url="https://www.psp.cz/sqw/sntisk.sqw?o=10", eid=f"psp:{k}", when=parse_date(h["seen"]),
                         detail="Úřední návrh v Poslanecké sněmovně – ověř ve zprávách, co přesně vláda navrhuje.")
              for k, h in hits.items()]
    return events, f"{mem.get('count', 0)} tisků, hlídané: {len(hits)}"


EASA_FEED = "https://www.easa.europa.eu/en/domains/air-operations/czibs/feed.xml"
EASA_WATCH = [   # (vzor v názvu, oblast, stupeň) – první shoda vyhrává
    {"match": r"czech|german", "region": None, "level": 3},
    {"match": r"poland|polish|slovak|austria|hungar|lithuan|latvia|eston|baltic|finland|romania|bulgaria|denmark|"
              r"norway|sweden|netherlands|belgium|france|italy|europe", "region": "EU", "level": 2},
    {"match": r"spain|spanish", "region": "ES", "level": 2},
    {"match": r"portug", "region": "PT", "level": 2},
    {"match": r"ireland|irish", "region": "IE", "level": 2},
    {"match": r"belarus|moldov|kaliningrad", "region": "EU", "level": 1},
]


def src_easa(cfg, fetch):
    """Bulletiny EASA o konfliktních zónách. Nový bulletin pro vzdušný prostor v Evropě = velmi vážný signál."""
    c = cfg["easa"]
    items = parse_feed(fetch(c.get("url", EASA_FEED)))
    watch = [(re.compile(w["match"]), w.get("region"), w["level"]) for w in c.get("sledovat", EASA_WATCH)]
    max_age = timedelta(days=c.get("max_age_days", 120))   # kanál obsahuje i stažené bulletiny
    events, recent = [], []
    for title, link, when in items:
        if when and utcnow() - when > max_age:
            continue
        recent.append({"title": title, "url": link, "time": iso(when) if when else ""})
        t = norm(title)
        for rx, region, lvl in watch:
            if rx.search(t):
                reg = region or ("CZ" if "czech" in t else "DE")
                events.append(make_event("EASA letectví", reg, lvl, f"EASA varuje letecké dopravce: {title}",
                                         url=link, eid=f"easa:{short_hash(link or title)}", when=when,
                                         detail="Bulletin o konfliktní zóně – riziko pro civilní letadla nad touto oblastí."))
                break
    return events, f"{len(items)} aktivních bulletinů, v Evropě: {len(events)}", {"czib": recent}


# ───────────────────────── mapa Evropy: incidenty ze zpráv po zemích ─────────────────────────

MAP_KAT = [   # (id, popis, vzor, kontext nutný k započtení, vyloučení) – na normalizovaný titulek
    ("drony", "Drony",
     r"\b(dron|drohn|drone|bezpilot|uav\b|shahed|geran)",
     r"\b(nad\b|over\b|uber\b|sobre\b|spatren|spozor|zahled|sichtung|gesichtet|sighting|sighted|narus|violat|incursion|"
     r"eindring|letist|airport|flughafen|aeropuert|aeroport|sestrel|shot down|abgeschossen|derrib|abatid|utok|zautoc|"
     r"attack|angriff|ataque|ataca|uzavr|zavr|closed|gesperrt|cerrad|fechad|vojensk|militar|zakladn|kaserne|base\b|"
     r"stihac|jets?\b|alarm|poplach|trosk|trummer|wreck|zasah|einschlag|strike)",
     r"\b(show|svetel|lichtshow|vanoc|weihnacht|zasilk|rozvoz|lieferung|paket|startup|nakup|kauf|buy|purchase|order|"
     r"zakazk|contract|vertrag|vyrob|produc|herstell|vyvoj|entwickl|tovarn|fabrik|factory|industr|prumysl|zemedel|"
     r"landwirt|farm|film|hollywood|dodav|liefer|deliver|zbrojovk|investic|invest)"),
    ("vzduch", "Vzdušný prostor a letiště",
     r"vzdusn[a-z]* prostor|airspace|luftraum|espacio aereo|espaco aereo|letist[a-z]* (uzavr|zavr|zastav)|"
     r"(uzavr|zavr)[a-z]* letist|airports? (closed|shut|suspend)|(closed|shut|suspend)[a-z]* (the )?airport|"
     r"flughafen[a-z]* (gesperrt|geschlossen|dicht)|flugbetrieb[a-z]* (eingestellt|unterbrochen)|aeropuerto[a-z]* cerrad|"
     r"scrambl|stihack[a-z]* (vzlet|odstart)|kampfjets? (aufgestiegen|gestartet)", None, None),
    ("utoky", "Útoky, výbuchy a sabotáže",
     r"\b(raket|missile|rakete|misil|missil|ostrel|beschuss|shelling|airstrike|air strike|leteck[a-z]* (utok|uder)|"
     r"luftangriff|raketenangriff|drohnenangriff|bombard|sabotaz|sabotage|sabotaje|anschlag|brandanschlag|terroranschlag|"
     r"bombenanschlag|sprengstoff|zhav|arson|explozi|explosion|exploz|terorist|terroris|terror attack|"
     r"undersea cable|unterseekabel|podmorsk[a-z]* kabel|cables? (cut|damaged|severed)|kabel[a-z]* (poskoz|beschadig|durchtrennt|zerstort))",
     None, r"\b(tenis|tennis|raketov[a-z]* start|spacex|nasa|vesmir|weltraum|space)"),
    ("blackout", "Blackout a výpadky proudu",
     r"\b(blackout|vypad(ek|ky|ku) (proudu|elektr)|bez proudu|bez elektriny|stromausfall|netzausfall|apagon|apagao|"
     r"power (outage|cut)|grid (failure|collapse))", None, None),
]
MAP_LABEL = {k: lbl for k, lbl, *_ in MAP_KAT}
_COUNTRY_RX = None


def country_patterns() -> dict:
    """Slovník zemí (zeme.json): kód → (název, regulární výraz na normalizovaný text)."""
    global _COUNTRY_RX
    if _COUNTRY_RX is None:
        data = json.loads((Path(__file__).with_name("zeme.json")).read_text(encoding="utf-8"))
        _COUNTRY_RX = {c: (v["nazev"], re.compile(r"\b(?:" + "|".join(v["vzory"]) + ")"))
                       for c, v in data.items() if v.get("vzory")}
    return _COUNTRY_RX


def countries_in(text: str) -> list:
    t = norm(text)
    return [c for c, (_, rx) in country_patterns().items() if rx.search(t)]


def map_categories(text: str) -> list:
    t = norm(text)
    out = []
    for k, _, rx, ctx, excl in MAP_KAT:
        if re.search(rx, t) and (not ctx or re.search(ctx, t)) and not (excl and re.search(excl, t)):
            out.append(k)
    return out


def src_mapa(cfg, fetch):
    """Kde se v Evropě podle zpráv něco děje: drony, útoky a sabotáže, vzdušný prostor, blackout (posledních 72 h).
    Zemi poznává podle názvů, přídavných jmen a měst. Sám stupeň doma nezvedá – jen kreslí mapu."""
    c = cfg["mapa"]
    now = utcnow()
    window = timedelta(hours=c.get("okno_hodin", 72))
    news_feeds = {f["id"]: f for f in (cfg.get("news") or {}).get("feeds", [])}
    feeds = []
    for f in c["feeds"]:
        f = {**news_feeds.get(f["id"], {}), **f}
        if f.get("url"):
            feeds.append(f)

    def one(f):
        return f, parse_feed(fetch(f["url"], timeout=20))

    zeme, seen, ok, failed = {}, set(), 0, []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in [ex.submit(one, f) for f in feeds]:
            try:
                f, items = fut.result()
            except Exception:
                failed.append("?")
                continue
            ok += 1
            for title, link, when in items:
                if when and now - when > window:
                    continue
                key = norm(title)
                if key in seen:
                    continue
                cats = map_categories(title)
                if not cats:
                    continue
                codes = countries_in(title) or ([f["zeme"]] if f.get("zeme") else [])
                if not codes or len(codes) > c.get("max_zemi_na_zpravu", 3):   # souhrnné zprávy o půlce Evropy nic neříkají
                    continue
                seen.add(key)
                for code in codes:
                    z = zeme.setdefault(code, {"kat": {}, "zpravy": []})
                    for k in cats:
                        z["kat"][k] = z["kat"].get(k, 0) + 1
                    z["zpravy"].append({"kat": cats, "title": title[:200], "url": link, "zdroj": f.get("name", f["id"]),
                                        "time": iso(when) if when else ""})
    if feeds and not ok:
        raise RuntimeError("žádný zpravodajský kanál pro mapu neodpověděl")
    for z in zeme.values():
        z["zpravy"].sort(key=lambda x: x["time"], reverse=True)
        z["zpravy"] = z["zpravy"][:c.get("max_zprav", 8)]
    total = sum(sum(z["kat"].values()) for z in zeme.values())
    top = sorted(zeme.items(), key=lambda kv: -sum(kv[1]["kat"].values()))[:6]
    info = (f"{ok}/{len(feeds)} kanálů, {len(zeme)} zemí se zmínkou, {total} zmínek za {c.get('okno_hodin', 72)} h"
            + (": " + ", ".join(f"{k} {sum(v['kat'].values())}" for k, v in top) if top else ""))
    return [], info, {"zeme": zeme, "time": iso(now), "okno_hodin": c.get("okno_hodin", 72)}


def europe_map(cfg, extras, regions, events=None) -> dict:
    """Stav zemí na mapě Evropy: úřední varování (AA, EASA), stupně hlídače a zmínky ve zprávách."""
    cp = country_patterns()
    c = cfg.get("mapa") or {}
    out = {}

    def add(code, lvl, why):
        if code not in cp:
            return
        z = out.setdefault(code, {"level": 0, "duvody": [], "kat": {}, "zpravy": []})
        z["level"] = max(z["level"], lvl)
        if why not in z["duvody"]:
            z["duvody"].append(why)

    for code, a in ((extras.get("aa") or {}).get("zeme") or {}).items():
        if a.get("warning"):
            add(code, 3, "Německé ministerstvo zahraničí varuje před cestami do celé země")
        elif a.get("partialWarning"):
            add(code, 2, "Německé ministerstvo zahraničí varuje před cestami do části země")
        elif a.get("situationWarning") or a.get("situationPartWarning"):
            add(code, 1, "Německé ministerstvo zahraničí upozorňuje na aktuální bezpečnostní situaci")
    for b in (extras.get("easa") or {}).get("czib") or []:
        for code in countries_in(b["title"]):
            add(code, 2, f"EASA varuje letecké dopravce: {b['title']}")
    for r in regions:
        if r["code"] in cp and r["level"]:
            add(r["code"], r["level"], f"Hlídač: stupeň {LEVELS[r['level']][0].capitalize()} – {r['events'][0]['title'] if r['events'] else r['name']}")
    for e in events or []:               # státní zdroje jednotlivých zemí (síť, věstníky, krizové weby)
        if e.get("zeme") and e["level"]:
            add(e["zeme"], e["level"], f"{e['source']}: {e['title']}")
    m = extras.get("mapa") or {}
    yellow = c.get("zluta_od_zminek", 3)
    for code, z in (m.get("zeme") or {}).items():
        if code not in cp:
            continue
        o = out.setdefault(code, {"level": 0, "duvody": [], "kat": {}, "zpravy": []})
        o["kat"], o["zpravy"] = z["kat"], z["zpravy"]
        n = len(z["zpravy"])
        if sum(z["kat"].values()) >= yellow and o["level"] < 1:
            o["level"] = 1
            o["duvody"].append(f"Ve zprávách za {m.get('okno_hodin', 72)} h víc zmínek: "
                               + ", ".join(f"{MAP_LABEL[k].lower()} {v}" for k, v in z["kat"].items()))
        o["n"] = n
    return {"zeme": out, "kategorie": [{"id": k, "nazev": lbl} for k, lbl, *_ in MAP_KAT],
            "time": m.get("time", ""), "okno_hodin": m.get("okno_hodin", 72)}


EMSC = "https://www.seismicportal.eu/fdsnws/event/1/query"
QUAKE_ADVICE = ("Po silném otřesu: zkontroluj plyn, komín a praskliny ve zdech, drž se pokynů HZS. "
                "Zemětřesné roje na Chebsku bývají slabé a k útěku obvykle není důvod.")


def src_emsc(cfg, fetch):
    """Zemětřesení v okolí domova (EMSC). Chebsko a Vogtland jsou nejaktivnější seismická oblast v Česku."""
    c = cfg["zemetreseni"]
    now = utcnow()
    start = (now - timedelta(days=c.get("dni", 7))).strftime("%Y-%m-%dT%H:%M:%S")
    quakes, events = [], []
    for z in c["zony"]:
        lon1, lat1, lon2, lat2 = z["bbox_lon_lat"]
        url = (f"{EMSC}?format=json&minlat={lat1}&maxlat={lat2}&minlon={lon1}&maxlon={lon2}"
               f"&minmag={c.get('min_mag', 2.0)}&starttime={start}&orderby=time&limit=50")
        raw = fetch(url, timeout=20)
        feats = json.loads(raw).get("features", []) if raw.strip() else []   # 204 = žádné zemětřesení
        for f in feats:
            p = f.get("properties") or {}
            mag, t = _num(p.get("mag")), parse_date(p.get("time"))
            if mag is None or not t:
                continue
            place = (p.get("flynn_region") or "").title()
            depth = _num(p.get("depth"))
            unid = p.get("unid") or f.get("id") or short_hash(f"{t}{mag}")
            quakes.append({"zone": z["name"], "mag": round(mag, 1), "time": iso(t), "place": place,
                           "depth": round(depth) if depth is not None else None})
            lvl = 2 if mag >= c.get("pripravit_od", 5.0) else 1 if mag >= c.get("sledovat_od", 3.5) else 0
            if lvl and now - t <= timedelta(hours=c.get("drzet_hodin", 24)):
                hl = f", hloubka {depth:.0f} km" if depth is not None else ""
                ev = make_event("EMSC zemětřesení", z.get("region", "CZ"), lvl,
                                f"Zemětřesení M{mag:.1f} – {z['name']} ({place}{hl})",
                                url=f"https://www.seismicportal.eu/eventdetails.html?unid={unid}",
                                eid=f"emsc:{unid}", when=t, detail=QUAKE_ADVICE)
                if lvl >= 2:
                    ev["advice"] = QUAKE_ADVICE
                events.append(ev)
    quakes.sort(key=lambda q: q["time"], reverse=True)
    strongest = max((q["mag"] for q in quakes), default=None)
    info = f"{len(quakes)} otřesů za {c.get('dni', 7)} dní" + (f", nejsilnější M{strongest}" if strongest else "")
    return events, info, {"quakes": quakes[:10], "days": c.get("dni", 7), "min_mag": c.get("min_mag", 2.0)}



# ───────────────────────── útočiště: státní zdroje (ES, PT, IE) ─────────────────────────

REE_RT = "https://apidatos.ree.es/es/datos/demanda/demanda-tiempo-real"
EIRGRID = "https://www.smartgriddashboard.com/DashboardService.svc/data"
BOE_API = "https://www.boe.es/datosabiertos/api/boe/sumario/"
OIREACHTAS = "https://api.oireachtas.ie/v1/legislation"
FOGOS = "https://api.fogos.pt/v2/incidents/active?all=1"
IGN_RSS = "https://www.ign.es/ign/RssTools/sismologia.xml"
IPMA_SEISMO = "https://api.ipma.pt/open-data/observation/seismic/7.json"
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
GRID_ADVICE = ("Výpadek proudu v zemi útočiště. Cestu tam teď neplánuj: nepojedou pumpy, platby kartou "
               "ani mobilní síť. Počkej, až provozovatel síť obnoví, nebo zvol jiné útočiště.")
GRID_EAST_ADVICE = ("Rozsáhlý výpadek proudu na východním křídle NATO. Může jít o poruchu, ale i o útok na síť – "
                    "sleduj, jestli se nešíří dál, a měj doma vodu, světlo a nabité powerbanky.")
GAZETTE_ADVICE = ("Úřední vyhlášení mimořádného stavu v zemi útočiště. Ověř, jestli platí omezení pohybu "
                  "nebo vstupu do země, než tam vyrazíš.")
ANEPC_ADVICE = ("Velké zásahy portugalské civilní ochrany. Před cestou ověř uzavírky silnic a evakuace "
                "na trase a v cíli (fogos.pt, prociv.gov.pt).")
BOE_RULES = [
    (2, r"(declara|prorroga)[a-z]* (el )?estado de (alarma|excepcion|sitio)"),
    (1, r"(modifica|amplia)[a-z]* (el )?estado de alarma|estado de (excepcion|sitio)"
        r"|situacion de interes para la seguridad nacional|movilizacion (general|militar|de reservistas)"
        r"|reservistas[a-z ]{0,40}(activa|incorpora)"),
]
BOE_EXCLUDE = r"^(correccion de errores|sentencia|auto |recurso|cuestion de inconstitucionalidad)"
ELI_API = "https://api.sejm.gov.pl/eli/acts/search"
VESTNESIS = "https://www.vestnesis.lv/feed/JL"
GAZETTE_EAST_ADVICE = ("Úřední vyhlášení mimořádného stavu nebo mobilizace v zemi na východním křídle NATO. "
                       "Zkontroluj zásoby, natankuj a sleduj, jestli se situace nešíří.")
PL_RULES = [
    (2, r"(wprowadz|oglosz|zarzadz)[a-z]* (stanu wojennego|stanu wojny|mobilizacji)|mobilizacji powszechnej"),
    (1, r"(wprowadz|przedluz)[a-z]* stanu (wyjatkowego|kleski zywiolowej)|stan[a-z]* wyjatkow[a-z]* na obszarze"
        r"|zakaz[a-z]* przebywania[a-z ]{0,40}(przygraniczn|stref)"),
]
PL_EXCLUDE = r"rocznic|pamiec|represjonowan|kombatan|1981|o zmianie ustawy|tekstu jednolitego"
LV_RULES = [
    (2, r"iznemuma stavokl|karastavokl|izsludin[a-z]* mobilizacij|mobilizacij[a-z]* izsludin"),
    (1, r"(izsludin|pagarin)[a-z]*.{0,40}arkarteja[a-z]* situacij|arkarteja[a-z]* situacij[a-z]*.{0,40}(izsludin|pagarin)"
        r"|robez[a-z]* .{0,30}(slegs|slegsan)"),
]
LV_EXCLUDE = r"grozijum[a-z]* .{0,20}likum|atzist par spetu zaudejus"
OIREACHTAS_RULES = [
    (2, r"emergency powers|national emergency"),
    (1, r"state of emergency|emergency measures|conscription|martial law|mobilis"
        r"|(electricity|energy|gas|fuel)[a-z ()]{0,30}emergency"),
]


def _tz(name: str, hours: int = 1):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=hours))


def grid_shortfall(points, thresholds, need=2):
    """Body [(čas, skutečnost, předpověď)] vzestupně. Stupeň platí, jen když ho dosáhnou poslední `need` body."""
    pts = [p for p in points if p[1] is not None and p[1] > 0 and p[2]]   # nula = výpadek dat, ne sítě
    if not pts:
        return 0, None, None
    def lvl(p):
        d = (p[2] - p[1]) / p[2] * 100
        return sum(1 for t in thresholds if d >= t)
    last = pts[-need:]
    level = min(lvl(p) for p in last) if len(last) >= need else 0
    t, a, f = pts[-1]
    return level, pts[-1], round((f - a) / f * 100, 1)


def ree_points(fetch, day) -> list:
    """REE: skutečná a předpovězená spotřeba Španělska po 5 minutách (MW)."""
    url = f"{REE_RT}?start_date={day:%Y-%m-%d}T00:00&end_date={day:%Y-%m-%d}T23:59&time_trunc=hour"
    js = json.loads(fetch(url, timeout=25))
    ser = {}
    for x in (js.get("included") or []) if isinstance(js, dict) else []:
        a = x.get("attributes") or {}
        ser[a.get("title") or x.get("type")] = a.get("values") or []
    if "Real" not in ser or "Prevista" not in ser:
        keys = list(ser)[:5] or (list(js)[:5] if isinstance(js, dict) else type(js).__name__)
        raise RuntimeError(f"REE: neznámý formát odpovědi {keys}")
    prev = {v.get("datetime"): _num(v.get("value")) for v in ser["Prevista"]}
    pts = []
    for v in ser["Real"]:
        t, val = parse_date(v.get("datetime")), _num(v.get("value"))
        if t and val is not None:
            pts.append((t, val, prev.get(v.get("datetime"))))
    return sorted(pts)


def eirgrid_rows(fetch, area, region, day, retry_s=8) -> list:
    """EirGrid Smart Grid Dashboard: řada [(čas, MW)] pro jeden den (irský místní čas).
    Server při více dotazech za sebou občas vrátí 503 – jeden opakovaný pokus po pauze."""
    ds = f"{day.day:02d}-{MONTHS_EN[day.month - 1]}-{day.year}"
    url = f"{EIRGRID}?area={area}&region={region}&datefrom={ds}+00%3A00&dateto={ds}+23%3A59"
    try:
        raw = fetch(url, timeout=25)
    except urllib.error.HTTPError as e:
        if e.code not in (429, 503) or not retry_s:
            raise
        time.sleep(retry_s)
        raw = fetch(url, timeout=25)
    js = json.loads(raw)
    if not isinstance(js, dict) or not isinstance(js.get("Rows"), list):
        raise RuntimeError(f"EirGrid: neznámý formát odpovědi {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
    if js.get("ErrorMessage"):
        raise RuntimeError(f"EirGrid: {js['ErrorMessage']}"[:150])
    tz = _tz("Europe/Dublin", 0)
    out = []
    for r in js["Rows"]:
        try:
            t = datetime.strptime(r.get("EffectiveTime", ""), "%d-%b-%Y %H:%M:%S").replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
        out.append((t, _num(r.get("Value"))))
    return out


PSE_LOAD = "https://api.raporty.pse.pl/api/kse-load"
ELERING = "https://dashboard.elering.ee/api/system/with-plan"
TRANSELECTRICA = "https://www.transelectrica.ro/sen-filter"


def pse_points(fetch, day) -> list:
    """PSE (Polsko): skutečné zatížení soustavy a předpověď po 15 minutách (MW)."""
    q = urllib.parse.quote(f"business_date eq '{day:%Y-%m-%d}'")
    js = json.loads(fetch(f"{PSE_LOAD}?$filter={q}&$first=200", timeout=25))
    rows = js.get("value") if isinstance(js, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"PSE: neznámý formát odpovědi {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
    pts = []
    for r in rows:
        t = parse_date(str(r.get("dtime_utc") or "").replace(" ", "T"))
        a = _num(r.get("load_actual"))
        if t and a is not None:
            pts.append((t, a, _num(r.get("load_fcst"))))
    return sorted(pts)


def elering_points(fetch, now) -> list:
    """Elering (Estonsko): skutečná spotřeba a plán po 15 minutách (MW)."""
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    js = json.loads(fetch(f"{ELERING}?start={(now - timedelta(hours=6)):{fmt}}&end={(now + timedelta(hours=1)):{fmt}}", timeout=25))
    d = js.get("data") if isinstance(js, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("real"), list):
        raise RuntimeError(f"Elering: neznámý formát odpovědi {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
    plan = {int(p["timestamp"]): _num(p.get("consumption")) for p in d.get("plan") or [] if _num(p.get("timestamp")) is not None}
    pts = []
    for r in d["real"]:
        ts, a = _num(r.get("timestamp")), _num(r.get("consumption"))
        if ts is not None and a is not None:
            pts.append((datetime.fromtimestamp(int(ts), timezone.utc), a, plan.get(int(ts))))
    return sorted(pts)


def transelectrica_now(fetch):
    """Transelectrica (Rumunsko): okamžitá spotřeba soustavy (MW). Předpověď nezveřejňuje."""
    js = json.loads(fetch(TRANSELECTRICA, timeout=25))
    if not isinstance(js, list):
        raise RuntimeError(f"Transelectrica: neznámý formát odpovědi {type(js).__name__}")
    d = {k: v for x in js if isinstance(x, dict) for k, v in x.items()}
    cons, ts = _num(d.get("CONS")), d.get("row1_HARTASEN_DATA")
    if cons is None or not ts:
        raise RuntimeError(f"Transelectrica: chybí spotřeba nebo čas {list(d)[:6]}")
    t = datetime.strptime(ts, "%y/%m/%d %H:%M:%S").replace(tzinfo=_tz("Europe/Bucharest", 2)).astimezone(timezone.utc)
    return t, cons


def own_baseline(hist: dict, t, tz, window_min=20, days=8):
    """Vlastní normál: medián spotřeby ze stejné denní doby (±`window_min`) v předchozích dnech stejného typu
    (pracovní den / víkend). Pro provozovatele, kteří předpověď nezveřejňují."""
    lt = t.astimezone(tz)
    mins, weekend = lt.hour * 60 + lt.minute, lt.weekday() >= 5
    vals = []
    for k, v in hist.items():
        kt = parse_date(k)
        if not kt or not timedelta(hours=20) <= t - kt <= timedelta(days=days):
            continue
        kl = kt.astimezone(tz)
        diff = abs(kl.hour * 60 + kl.minute - mins)
        if min(diff, 1440 - diff) <= window_min and (kl.weekday() >= 5) == weekend:
            vals.append(v)
    if len(vals) < 2:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def src_grid_refuges(cfg, fetch, mem):
    """Elektrická síť v zemích útočišť a na východním křídle: skutečná spotřeba proti předpovědi provozovatele.
    Prudký propad pod předpověď = rozsáhlý výpadek (tak se projevil blackout na Pyrenejském poloostrově 28. 4. 2025)."""
    c = cfg["sit_utocista"]
    now = utcnow()
    stale_after = timedelta(minutes=c.get("stara_data_min", 90))
    out, events, errors = [], [], []
    for s in c["systemy"]:
        key = s.get("zeme") or s["region"]
        thr = s.get("pokles_pct") or c.get("pokles_pct", [15, 35])
        try:
            if s["typ"] == "pse":
                day = now.astimezone(_tz("Europe/Warsaw")).date()
                pts = pse_points(fetch, day)
                if len(pts) < 2:
                    pts = pse_points(fetch, day - timedelta(days=1)) + pts
                operator, link = "PSE", "https://raporty.pse.pl/"
            elif s["typ"] == "elering":
                pts = elering_points(fetch, now)
                operator, link = "Elering", "https://dashboard.elering.ee/et/system"
            elif s["typ"] == "transelectrica":
                t, cons = transelectrica_now(fetch)
                tz = _tz("Europe/Bucharest", 2)
                hist = mem.setdefault("historie", {}).setdefault(key, {})
                hist[iso(t.replace(second=0, microsecond=0))] = cons
                for k in [k for k in hist if now - (parse_date(k) or now) > timedelta(days=9)]:
                    del hist[k]
                recent = sorted((parse_date(k), v) for k, v in hist.items() if now - (parse_date(k) or now) <= timedelta(hours=1))
                pts = [(tt, v, own_baseline(hist, tt, tz)) for tt, v in recent]
                operator, link = "Transelectrica", "https://www.transelectrica.ro/web/tel/sen-harta"
            elif s["typ"] == "ree":
                day = now.astimezone(_tz("Europe/Madrid")).date()
                pts = ree_points(fetch, day)
                if len([p for p in pts if p[1]]) < 2:          # krátce po půlnoci ještě nejsou dnešní data
                    pts = ree_points(fetch, day - timedelta(days=1)) + pts
                operator, link = "REE", "https://www.ree.es/es/datos/demanda"
            elif s["typ"] == "eirgrid":
                reg = s.get("oblast", "ROI")
                day = now.astimezone(_tz("Europe/Dublin", 0)).date()
                fc = mem.setdefault("eirgrid_fc", {}).setdefault(reg, {})
                due_key = f"eirgrid_fc_cas_{reg}"
                if _due(mem, due_key, c.get("predpoved_kazdych_h", 1), now) or not any(
                        parse_date(k) and parse_date(k) > now for k in fc):
                    try:      # předpověď je jen dopředu → schovat si ji; stačí jednou za hodinu
                        for t, v in eirgrid_rows(fetch, "demandforecast", reg, day):
                            if v:
                                fc[iso(t)] = v
                        mem[due_key] = iso(now)
                    except Exception as e:
                        if not fc:
                            raise
                        errors.append(f"{s.get('name', reg)} předpověď: {type(e).__name__}"[:80])
                for k in [k for k in fc if now - (parse_date(k) or now) > timedelta(hours=36)]:
                    del fc[k]
                act = [(t, v) for t, v in eirgrid_rows(fetch, "demandactual", reg, day) if v is not None]
                if len(act) < 2:
                    act = [(t, v) for t, v in eirgrid_rows(fetch, "demandactual", reg, day - timedelta(days=1))
                           if v is not None] + act
                pts = []
                for t, v in act:
                    slot = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)
                    pts.append((t, v, fc.get(iso(slot))))
                operator, link = "EirGrid", "https://www.smartgriddashboard.com/roi/demand/"
            else:
                raise RuntimeError(f"neznámý typ sítě {s['typ']}")
        except Exception as e:
            errors.append(f"{s.get('name', key)}: {type(e).__name__}: {e}"[:160])
            prev = mem.get("posledni", {}).get(key)      # krátký výpadek provozovatele: drž poslední stav
            if prev and now - (parse_date(prev["cas"]) or now) <= timedelta(minutes=c.get("drzet_pri_vypadku_min", 120)):
                out.append({**prev["item"], "pozn": "poslední známý stav, provozovatel teď neodpovídá"})
                events += prev["events"]
            continue
        level, last, dev = grid_shortfall(pts, thr)
        item = {"code": key, "region": s["region"], "name": s.get("name", key), "operator": operator, "level": 0}
        if last:
            stale = now - last[0] > stale_after
            item.update(actual=round(last[1]), forecast=round(last[2]), dev=dev, time=iso(last[0]), stale=stale)
            if stale:
                level = 0
        else:
            item["pozn"] = ("normál se teprve sbírá" if s["typ"] == "transelectrica" else "předpověď se teprve sbírá") \
                if pts else "zatím bez dat"
        item["level"] = level
        evs = []
        if level:
            ev = make_event(f"Síť {operator}", s["region"], level,
                            f"Možný výpadek proudu – {item['name']}: spotřeba {last[1] / 1000:.1f} GW, "
                            f"o {dev:.0f} % pod předpovědí {last[2] / 1000:.1f} GW ({operator}).".replace(".", ",", 2),
                            url=link, eid=f"sit:{key}:{level}", when=last[0],
                            detail=GRID_ADVICE if s["region"] != "EU" else GRID_EAST_ADVICE)
            if s.get("zeme"):
                ev["zeme"] = s["zeme"]
            if level >= 2:
                ev["advice"] = ev["detail"]
            evs.append(ev)
        mem.setdefault("posledni", {})[key] = {"cas": iso(now), "item": item, "events": evs}
        events += evs
        out.append(item)
    if not out:
        raise RuntimeError("; ".join(errors) or "žádná síť není nastavená")
    parts = [f"{o['code']} " + (f"{o['actual'] / 1000:.1f} GW ({-o['dev']:+.0f} % proti předpovědi)".replace(".", ",")
                                if "actual" in o else o.get("pozn", "")) for o in out]
    info = "; ".join(parts) + (f"; nefunguje: {'; '.join(errors)}" if errors else "")
    return events, info, {"site": out, "time": iso(now)}


def _aslist(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def _boe_walk(o):
    if isinstance(o, list):
        for x in o:
            yield from _boe_walk(x)
    elif isinstance(o, dict):
        if o.get("identificador") and o.get("titulo"):
            yield o
            return
        for v in o.values():
            if isinstance(v, (dict, list)):
                yield from _boe_walk(v)


def boe_items(js) -> list:
    """Položky souhrnu BOE jako (kód oddílu, položka). Úřad i nadpis mohou být seznam i jediný objekt."""
    sumario = ((js.get("data") or {}) if isinstance(js, dict) else {}).get("sumario") or {}
    out = []
    for diario in _aslist(sumario.get("diario")):
        for sec in _aslist(diario.get("seccion") if isinstance(diario, dict) else None):
            if isinstance(sec, dict):
                out += [(str(sec.get("codigo") or ""), it) for it in _boe_walk(sec)]
    return out


def _match_level(title: str, rules) -> int:
    t = norm(title)
    return max([lvl for lvl, rx in rules if re.search(rx, t)] or [0])


def src_gazettes(cfg, fetch, mem):
    """Úřední věstníky a parlamenty: BOE (Španělsko), návrhy zákonů Oireachtas (Irsko), Dziennik Ustaw a Monitor
    Polski (Polsko, ELI API Sejmu) a Latvijas Vēstnesis (Lotyšsko). Vyhlášení stavu nouze, výjimečného
    nebo válečného stavu či mobilizace se tu objeví úředně dřív, než se rozkřikne."""
    c = cfg["vestniky"]
    now = utcnow()
    active = mem.setdefault("active", {})
    info, errors, parts = [], [], {}

    def add(key, title, url, level, src, region, zeme=None):
        if key not in active:
            active[key] = {"title": title, "url": url, "level": level, "src": src, "region": region, "first": iso(now)}
            if zeme:
                active[key]["zeme"] = zeme

    if c.get("boe", True):
        try:
            today = now.astimezone(_tz("Europe/Madrid")).date()
            n, days = 0, []
            for d in (today, today - timedelta(days=1)):
                try:
                    raw = fetch(f"{BOE_API}{d:%Y%m%d}", timeout=30, headers={"Accept": "application/json"})
                except urllib.error.HTTPError as e:
                    if e.code == 404:          # ten den BOE nevyšel (neděle) nebo ještě nevyšel
                        continue
                    raise
                items = boe_items(json.loads(raw))
                if not items:
                    raise RuntimeError("BOE: v souhrnu nejsou položky – změnil se formát?")
                days.append(d)
                n += len(items)
                for sec, it in items:     # vyhlášení vychází v oddílu I; rozsudky o dřívějších stavech ne
                    if sec not in c.get("boe_oddily", ["1"]) or re.search(BOE_EXCLUDE, norm(it["titulo"])):
                        continue
                    lvl = _match_level(it["titulo"], BOE_RULES)
                    if lvl:
                        url = it.get("url_html") or ((it.get("url_pdf") or {}).get("texto") if isinstance(it.get("url_pdf"), dict) else it.get("url_pdf")) or "https://www.boe.es/"
                        add(f"boe:{it['identificador']}", it["titulo"], url, lvl, "BOE", "ES")
            parts["boe"] = {"n": n, "dny": [d.isoformat() for d in days]}
            info.append(f"BOE {n} položek" + (f" ({', '.join(fmt_date(d.isoformat()) for d in days)})" if days else ", dnes ani včera nevyšel"))
        except Exception as e:
            errors.append(f"BOE: {type(e).__name__}: {e}"[:150])
    if c.get("oireachtas", True):
        try:
            since = (now - timedelta(days=c.get("oireachtas_dni", 14))).strftime("%Y-%m-%d")
            js = json.loads(fetch(f"{OIREACHTAS}?date_start={since}&limit=200", timeout=30))
            if not isinstance(js, dict) or not isinstance(js.get("results"), list):
                raise RuntimeError(f"Oireachtas: neznámý formát {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
            n = 0
            for r in js["results"]:
                b = r.get("bill") or {}
                title = (b.get("shortTitleEn") or "").strip()
                if not title:
                    continue
                n += 1
                lvl = _match_level(title, OIREACHTAS_RULES)
                if lvl:
                    y, no = b.get("billYear"), b.get("billNo")
                    add(f"oir:{y}/{no}", f"{title} ({b.get('status') or 'návrh'})",
                        f"https://www.oireachtas.ie/en/bills/bill/{y}/{no}/", lvl, "Oireachtas", "IE")
            parts["oireachtas"] = {"n": n}
            info.append(f"Oireachtas {n} návrhů za {c.get('oireachtas_dni', 14)} dní")
        except Exception as e:
            errors.append(f"Oireachtas: {type(e).__name__}: {e}"[:150])
    if c.get("eli_pl", False):
        try:
            since = (now - timedelta(days=c.get("eli_dni", 7))).strftime("%Y-%m-%d")
            n = 0
            for pub in c.get("eli_vydani", ["DU", "MP"]):
                js = json.loads(fetch(f"{ELI_API}?publisher={pub}&pubDateFrom={since}&limit=500", timeout=30))
                items = js.get("items") if isinstance(js, dict) else None
                if not isinstance(items, list):
                    raise RuntimeError(f"ELI: neznámý formát {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
                for it in items:
                    title = it.get("title") or ""
                    n += 1
                    if not title or re.search(PL_EXCLUDE, norm(title)):
                        continue
                    lvl = _match_level(title, PL_RULES)
                    if lvl and it.get("ELI"):
                        add(f"eli:{it['ELI']}", f"{it.get('displayAddress') or it['ELI']}: {title}",
                            f"https://eli.gov.pl/eli/{it['ELI']}/ogl", lvl,
                            "Dziennik Ustaw" if pub == "DU" else "Monitor Polski", "EU", "PL")
            parts["eli_pl"] = {"n": n}
            info.append(f"Polsko {n} aktů za {c.get('eli_dni', 7)} dní")
        except Exception as e:
            errors.append(f"ELI Polsko: {type(e).__name__}: {e}"[:150])
    if c.get("vestnesis_lv", False):
        try:
            items = parse_feed(fetch(c.get("vestnesis_url", VESTNESIS), timeout=25))
            for title, link, _ in items:
                if re.search(LV_EXCLUDE, norm(title)):
                    continue
                lvl = _match_level(title, LV_RULES)
                if lvl:
                    add(f"lv:{short_hash((link or '') + title)}", title, link or "https://www.vestnesis.lv/",
                        lvl, "Latvijas Vēstnesis", "EU", "LV")
            parts["vestnesis_lv"] = {"n": len(items)}
            info.append(f"Lotyšsko {len(items)} aktů v posledním čísle")
        except Exception as e:
            errors.append(f"Latvijas Vēstnesis: {type(e).__name__}: {e}"[:150])
    if not parts:
        raise RuntimeError("; ".join(errors) or "žádný věstník není zapnutý")
    hold = timedelta(days=c.get("drzet_dni", 7))
    for k in [k for k, a in active.items() if now - (parse_date(a["first"]) or now) > hold]:
        del active[k]
    events = []
    for k, a in active.items():
        adv = GAZETTE_EAST_ADVICE if a["region"] == "EU" else GAZETTE_ADVICE
        ev = make_event(a["src"], a["region"], a["level"], f"{a['src']}: {a['title']}", url=a["url"],
                        eid=f"vestnik:{k}", when=parse_date(a["first"]), detail=adv)
        if a.get("zeme"):
            ev["zeme"] = a["zeme"]
        if a["level"] >= 2:
            ev["advice"] = adv
        events.append(ev)
    info = ", ".join(info) + f"; zachyceno: {len(active)}" + (f"; nefunguje: {'; '.join(errors)}" if errors else "")
    return events, info, {**parts, "zachyceno": [{**a, "key": k} for k, a in active.items()][:10]}


def src_anepc(cfg, fetch, mem):
    """Portugalská civilní ochrana (ANEPC): probíhající zásahy přes fogos.pt, který data ANEPC zveřejňuje strojově.
    fogos.pt omezuje počet dotazů (HTTP 429): stahuje se jednou za `kazdych_min` a při odmítnutí platí poslední data."""
    c = cfg["anepc"]
    now = utcnow()
    last = mem.get("vysledek")
    last_t = parse_date(mem.get("cas"))
    def cached(note):
        ev, info, extra = last
        return ev, info + note, extra
    if last and not _due(mem, "cas", c.get("kazdych_min", 20) / 60, now):
        return cached("")
    try:
        res = anepc_eval(c, fetch(c.get("url", FOGOS), timeout=25), now)
    except Exception as e:
        if last and last_t and now - last_t <= timedelta(hours=c.get("drzet_hodin", 2)):
            why = "omezuje dotazy" if isinstance(e, urllib.error.HTTPError) and e.code == 429 else "neodpovídá"
            return cached(f" (data z {last_t.astimezone(local_tz(cfg)):%H:%M}, fogos.pt teď {why})")
        raise
    mem["vysledek"], mem["cas"] = list(res), iso(now)
    return res


def anepc_eval(c, raw, now):
    js = json.loads(raw)
    if not isinstance(js, dict) or not isinstance(js.get("data"), list):
        raise RuntimeError(f"fogos.pt: neznámý formát odpovědi {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
    inc = [i for i in js["data"] if isinstance(i, dict)]
    def n(i, k):
        return int(_num(i.get(k)) or 0)
    total = sum(n(i, "man") for i in inc)
    big = [i for i in inc if i.get("important") or n(i, "man") >= c.get("velky_zasah_lidi", 150)
           or n(i, "aerial") >= c.get("velky_zasah_letadel", 4)]
    big.sort(key=lambda i: (-n(i, "man"), -n(i, "aerial")))
    def place(i):
        return i.get("concelho") or i.get("district") or i.get("location") or "?"
    def kind(i):
        k = re.sub(r"\s+", " ", str(i.get("natureza") or "")).strip()
        return k[:48] + ("…" if len(k) > 48 else "")
    lvl = 2 if total >= c.get("pripravit_lidi_celkem", 2500) else 1 if big else 0
    events = []
    if lvl:
        top = ", ".join(f"{place(i)} ({n(i, 'man')} lidí"
                        + (f", {_pl(n(i, 'aerial'), 'letadlo', 'letadla', 'letadel')}" if n(i, "aerial") else "") + ")"
                        for i in big[:3])
        title = (f"Portugalsko – {_pl(len(big), 'velký zásah', 'velké zásahy', 'velkých zásahů')} ANEPC, "
                 f"celkem {total} lidí v terénu"
                 + (f": {top}" if top else ""))
        ev = make_event("ANEPC", c.get("region", "PT"), lvl, title, url="https://fogos.pt/",
                        eid=f"anepc:L{lvl}", detail=ANEPC_ADVICE)
        if lvl >= 2:
            ev["advice"] = ANEPC_ADVICE
        events.append(ev)
    rows = []
    for i in (big or sorted(inc, key=lambda i: -n(i, "man")))[:6]:
        t = _num((i.get("dateTime") or {}).get("sec")) if isinstance(i.get("dateTime"), dict) else None
        rows.append({"misto": place(i), "okres": i.get("district") or "", "druh": kind(i), "lidi": n(i, "man"),
                     "letadla": n(i, "aerial"), "stav": i.get("status") or "", "velky": i in big,
                     "time": iso(datetime.fromtimestamp(t, timezone.utc)) if t else ""})
    info = f"{len(inc)} zásahů, {total} lidí v terénu, velkých: {len(big)}"
    return events, info, {"n": len(inc), "lidi": total, "velke": len(big), "zasahy": rows, "level": lvl, "time": iso(now)}


def _pl(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


OKF_SUGARZAS = "https://www.katasztrofavedelem.hu/10464/RSS_hattersugarzas"
RAD_EAST_ADVICE = ("Zvýšená radiace na více sondách v Maďarsku (u Paksu je jaderná elektrárna). Sleduj vývoj a směr větru; "
                   "měj připravené jódové tablety jen podle pokynů úřadů a plán, kde se ukrýt.")


def src_radiation_hu(cfg, fetch):
    """Radiace v Maďarsku: dávkový příkon z měřicí sítě maďarské civilní ochrany (BM OKF), asi 80 stanic."""
    c = cfg["radiace_hu"]
    root = ET.fromstring(fetch(c.get("url", OKF_SUGARZAS), timeout=25))
    latest = {}
    for it in root.iter("item"):
        title, desc = child_text(it, "title"), child_text(it, "description")
        m = re.search(r"([\d]+(?:[.,]\d+)?)\s*nsv/h", desc, re.I)
        st = re.search(r":\s*(.+?)\s+\d{4}-\d{2}-\d{2}", title)
        t = parse_date(child_text(it, "pubDate"))
        if not m or not st:
            continue
        name, val = st.group(1).strip(), float(m.group(1).replace(",", "."))
        if name not in latest or (t and latest[name][1] and t > latest[name][1]):
            latest[name] = (val, t)
    if not latest:
        raise RuntimeError("BM OKF: v kanálu nejsou měření – změnil se formát?")
    vals = sorted(v for v, _ in latest.values())
    warn, alarm = c.get("warn_nsvh", 600), c.get("alarm_nsvh", 2000)
    n_warn = sum(1 for v in vals if v >= warn)
    n_alarm = sum(1 for v in vals if v >= alarm)
    need = c.get("min_stanic", 2)
    lvl = 2 if n_alarm >= need else 1 if n_warn >= need else 0
    top_name, (top_val, top_t) = max(latest.items(), key=lambda kv: kv[1][0])
    newest = max((t for _, t in latest.values() if t), default=None)
    events = []
    if lvl:
        ev = make_event("BM OKF radiace", c.get("region", "EU"), lvl,
                        f"Zvýšená radiace v Maďarsku: {n_alarm if lvl >= 2 else n_warn} stanic nad "
                        f"{(alarm if lvl >= 2 else warn) / 1000:.1f} µSv/h, nejvíc {top_name} {top_val / 1000:.2f} µSv/h".replace(".", ","),
                        url="https://www.katasztrofavedelem.hu/modules/hattersugarzas/terkep",
                        eid=f"radhu:{lvl}", detail=RAD_EAST_ADVICE)
        ev["zeme"] = c.get("zeme", "HU")
        if lvl >= 2:
            ev["advice"] = RAD_EAST_ADVICE
        events.append(ev)
    median = vals[len(vals) // 2]
    info = f"{len(vals)} stanic, medián {median / 1000:.2f} µSv/h, nejvíc {top_name} {top_val / 1000:.2f} µSv/h"
    zone = {"name": "Maďarsko (BM OKF)", "n": len(vals), "median": round(median / 1000, 3),
            "max": round(top_val / 1000, 3), "max_name": top_name, "level": lvl, "time": iso(newest) if newest else ""}
    return events, info, {"zona": zone, "warn": warn / 1000, "alarm": alarm / 1000}


def _in_bbox(lat, lon, bb) -> bool:
    lon1, lat1, lon2, lat2 = bb
    return lat1 <= lat <= lat2 and lon1 <= lon <= lon2


def src_seismo_refuges(cfg, fetch):
    """Zemětřesení na Pyrenejském poloostrově podle státních seismologických služeb (IGN Španělsko, IPMA Portugalsko)."""
    c = cfg["zemetreseni_utocista"]
    now = utcnow()
    days = c.get("dni", 7)
    bbox = c.get("oblasti", [[-10.0, 35.8, 4.5, 44.0]])
    pt_box = c.get("portugalsko_bbox", [-9.8, 36.8, -6.9, 42.2])
    found, errors, ok = [], [], []

    def keep(src, lat, lon, mag, t, place, url):
        if None in (lat, lon, mag) or not t or mag < -9 or now - t > timedelta(days=days):
            return
        if not any(_in_bbox(lat, lon, b) for b in bbox):
            return
        found.append({"src": src, "lat": lat, "lon": lon, "mag": round(mag, 1), "time": iso(t), "_t": t,
                      "place": place, "url": url, "region": "PT" if _in_bbox(lat, lon, pt_box) else "ES"})

    if c.get("ign", True):
        try:
            root = ET.fromstring(fetch(c.get("ign_url", IGN_RSS), timeout=25))
            n = 0
            for it in root.iter("item"):
                n += 1
                d = child_text(it, "description")
                m = re.search(r"magnitud\s+([\d.,]+)\s+en\s+(.+?)\s+en la fecha\s+(\d+/\d+/\d+\s+\d+:\d+:\d+)", d)
                if not m:
                    continue
                try:
                    t = datetime.strptime(m.group(3), "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                lat = lon = None
                for ch in it:
                    if local(ch.tag) == "lat":
                        lat = _num(ch.text)
                    elif local(ch.tag) == "long":
                        lon = _num(ch.text)
                keep("IGN", lat, lon, _num(m.group(1).replace(",", ".")), t, m.group(2).strip().title(),
                     child_text(it, "link") or "https://www.ign.es/web/ign/portal/ultimos-terremotos")
            if n and not any(f["src"] == "IGN" for f in found) and "magnitud" not in ET.tostring(root, encoding="unicode"):
                raise RuntimeError("IGN: neznámý formát položek")
            ok.append(f"IGN {n}")
        except Exception as e:
            errors.append(f"IGN: {type(e).__name__}: {e}"[:150])
    if c.get("ipma", True):
        try:
            js = json.loads(fetch(c.get("ipma_url", IPMA_SEISMO), timeout=25))
            data = js.get("data") if isinstance(js, dict) else None
            if not isinstance(data, list):
                raise RuntimeError(f"IPMA: neznámý formát {list(js)[:5] if isinstance(js, dict) else type(js).__name__}")
            for q in data:
                keep("IPMA", _num(q.get("lat")), _num(q.get("lon")), _num(q.get("magnitud")), parse_date(q.get("time")),
                     (q.get("obsRegion") or "").strip().title(), "https://www.ipma.pt/pt/geofisica/sismologia/")
            ok.append(f"IPMA {len(data)}")
        except Exception as e:
            errors.append(f"IPMA: {type(e).__name__}: {e}"[:150])
    if not ok:
        raise RuntimeError("; ".join(errors))
    found.sort(key=lambda q: (q["_t"], q["src"] != "IPMA"))
    quakes = []
    for q in found:      # stejný otřes od obou služeb: do 2 minut a 0,5°
        dup = next((x for x in quakes if abs((x["_t"] - q["_t"]).total_seconds()) <= 120
                    and abs(x["lat"] - q["lat"]) <= 0.5 and abs(x["lon"] - q["lon"]) <= 0.5), None)
        if dup:
            dup["mag"] = max(dup["mag"], q["mag"])
            continue
        quakes.append(q)
    events = []
    for q in quakes:
        lvl = 2 if q["mag"] >= c.get("pripravit_od", 6.0) else 1 if q["mag"] >= c.get("sledovat_od", 4.5) else 0
        if lvl and now - q["_t"] <= timedelta(hours=c.get("drzet_hodin", 24)):
            ev = make_event(f"{q['src']} zemětřesení", q["region"], lvl,
                            f"Zemětřesení M{q['mag']:.1f} – {q['place']} ({q['src']})", url=q["url"],
                            eid=f"seis:{q['src']}:{q['time']}", when=q["_t"], detail=QUAKE_ADVICE)
            if lvl >= 2:
                ev["advice"] = QUAKE_ADVICE
            events.append(ev)
    quakes.sort(key=lambda q: q["_t"], reverse=True)
    shown = [{k: v for k, v in q.items() if k != "_t"} for q in quakes if q["mag"] >= c.get("min_mag", 2.5)][:10]
    strongest = max((q["mag"] for q in quakes), default=None)
    info = (f"{len(quakes)} otřesů za {days} dní" + (f", nejsilnější M{strongest}" if strongest is not None else "")
            + f" ({', '.join(ok)})" + (f"; nefunguje: {'; '.join(errors)}" if errors else ""))
    return events, info, {"quakes": shown, "days": days, "min_mag": c.get("min_mag", 2.5)}


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
    "gas":            ("Zásobníky plynu (AGSI)", src_gas, "gas"),
    "lng":            ("LNG terminály (ALSI)", src_lng, "lng"),
    "gasout":         ("Výpadky plynovodů a zásobníků (GIE)", src_gas_outages, "plyn_vypadky"),
    "mapa":           ("Mapa Evropy – zprávy po zemích", src_mapa, "mapa"),
    "ioda":           ("Výpadky internetu (IODA)", src_ioda, "internet"),
    "gdelt":          ("Objem světových zpráv (GDELT)", src_gdelt, "gdelt"),
    "nukib":          ("NÚKIB – kybernetická varování", src_nukib, "nukib"),
    "psp":            ("Sněmovní tisky (PSP)", src_psp, "psp"),
    "easa":           ("EASA – konfliktní zóny", src_easa, "easa"),
    "emsc":           ("Zemětřesení (EMSC)", src_emsc, "zemetreseni"),
    "sit":            ("Elektrická síť (útočiště a východní křídlo)", src_grid_refuges, "sit_utocista"),
    "vestniky":       ("Úřední věstníky (BOE, Oireachtas, Polsko, Lotyšsko)", src_gazettes, "vestniky"),
    "anepc":          ("Civilní ochrana Portugalska (ANEPC)", src_anepc, "anepc"),
    "seismo_ib":      ("Zemětřesení – IGN a IPMA", src_seismo_refuges, "zemetreseni_utocista"),
    "radiace_hu":     ("Radiace – Maďarsko (BM OKF)", src_radiation_hu, "radiace_hu"),
}


# ───────────────────────── zprávy (RSS) ─────────────────────────

def parse_feed(raw: bytes):
    root = ET.fromstring(raw.lstrip(b"\xef\xbb\xbf \r\n\t"))     # některé weby posílají před deklarací prázdný řádek
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
                            if feed.get("zeme"):
                                hits[key]["zeme"] = feed["zeme"]
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
        if r.get("zeme") or rh[0].get("zeme"):
            ev["zeme"] = r.get("zeme") or rh[0]["zeme"]
        events.append(ev)
    return events, health


# ───────────────────────── připravenost (doklady, lhůty, seznamy) ─────────────────────────

DEFAULT_CHECKLISTS = [
    {"id": "zasoby", "nazev": "Nouzové zásoby na 72 hodin", "polozky": [
        "Pitná voda – 2 litry na osobu a den, na 3 dny", "Kanystry nebo jiné uzavíratelné nádoby na vodu",
        "Trvanlivé jídlo, které nemusíš vařit", "Lékárnička a pravidelné léky aspoň na týden",
        "Rádio a svítilna na baterie, náhradní baterie", "Hotovost v mincích a menších bankovkách",
        "Nabité powerbanky", "Plynový vařič a kartuše", "Hygienické potřeby a pytle na odpad",
        "Kopie důležitých dokumentů"]},
    {"id": "zavazadlo", "nazev": "Evakuační zavazadlo u dveří", "polozky": [
        "Doklady všech, platební karty, hotovost v CZK i EUR", "Lékárnička a léky",
        "Telefon, nabíječka a powerbanka", "Rádio a svítilna", "Voda a jídlo na cestu",
        "Teplé a náhradní oblečení, pláštěnka, pevné boty", "Deka nebo spacák", "Hygienické potřeby",
        "Zavazadlo označené jménem a telefonem"]},
    {"id": "dite", "nazev": "Pro malého", "polozky": [
        "Pleny a vlhčené ubrousky na několik dní", "Jídlo, pití a lahvička nebo hrneček",
        "Náhradní oblečení a teplá vrstva", "Dětské léky podle doporučení lékaře", "Oblíbená hračka nebo knížka",
        "Kartička se jmény a telefony rodičů (pro případ, že se rozdělíte)", "Autosedačka a nosítko"]},
    {"id": "auto", "nazev": "Auto", "polozky": [
        "Nádrž aspoň napůl, při stupni Sleduj do plna", "Kanystr na palivo (podle pravidel v zemích po cestě)",
        "Lékárnička, reflexní vesty pro všechny, trojúhelník", "Nálepka Crit'Air a registrace do ZBE (viz výše)",
        "Tištěná mapa Evropy", "Tažné lano a startovací kabely", "Doklady k autu a zelená karta"]},
    {"id": "telefon", "nazev": "Telefon a spojení", "polozky": [
        "Zapnutá nouzová (bezdrátová) upozornění – na iPhonu v Nastavení → Oznámení",
        "Aplikace NINA (Německo) a Záchranka (Česko)", "Trasy uložené do offline map (tlačítko GPX u trasy)",
        "Domluvené místo srazu pro případ, že nefunguje signál", "Důležitá čísla zapsaná i na papíře",
        "Tento web přidaný na plochu – ukáže poslední stav i bez signálu"]},
]
PREP_LABEL = {"propadle": "propadlé", "brzy": "brzy končí", "chybi_datum": "chybí datum", "nevyrizeno": "nevyřízeno",
              "volitelne": "až bude potřeba", "ok": "v pořádku"}


def fmt_date(s) -> str:
    try:
        d = date.fromisoformat(str(s))
        return f"{d.day}. {d.month}. {d.year}"
    except ValueError:
        return str(s or "")


def preparedness(cfg, now) -> list:
    """Stav položek z `pripravenost.polozky`: buď mají `plati_do` (hlídá se platnost), nebo `hotovo` (jednorázový úkol)."""
    c = cfg.get("pripravenost") or {}
    today = now.astimezone(local_tz(cfg)).date()
    warn = c.get("varovat_dni", 60)
    out = []
    for p in c.get("polozky", []):
        days = None
        if "plati_do" in p:
            try:
                d = date.fromisoformat(str(p["plati_do"])) if p.get("plati_do") else None
            except ValueError:
                d = None
            if d is None:
                st = "chybi_datum"
            else:
                days = (d - today).days
                st = "propadle" if days < 0 else "brzy" if days <= warn else "ok"
        elif p.get("hotovo"):
            st = "ok"
        else:
            st = "volitelne" if p.get("volitelne") else "nevyrizeno"
        out.append({"id": p.get("id") or short_hash(p.get("nazev", "")), "nazev": p.get("nazev", ""),
                    "druh": "platnost" if "plati_do" in p else "ukol",
                    "pozn": p.get("pozn", ""), "odkaz": p.get("odkaz", ""), "plati_do": p.get("plati_do"),
                    "dni": days, "stav": st, "stav_text": PREP_LABEL[st]})
    return out


def prep_label(i) -> str:
    if i["stav"] == "propadle":
        return f"{i['nazev']} – propadlé od {fmt_date(i['plati_do'])}"
    if i["stav"] == "brzy":
        return f"{i['nazev']} – platí ještě {i['dni']} dní (do {fmt_date(i['plati_do'])})"
    return f"{i['nazev']} – {PREP_LABEL[i['stav']]}"


def prep_problems(items, limit=4) -> list:
    """Co by tě zdrželo při odjezdu: propadlé, končící do 30 dní a nevyřízené."""
    bad = [i for i in items if i["stav"] in ("propadle", "nevyrizeno")
           or (i["stav"] == "brzy" and i["dni"] is not None and i["dni"] <= 30)]
    return [prep_label(i) for i in bad][:limit]


def remind_preparedness(cfg, state, items, now):
    """Připomínky platnosti (výchozí 60, 30, 7 dní a den konce) – jedna souhrnná zpráva."""
    c = cfg.get("pripravenost") or {}
    thresholds = c.get("upozornit_dni", [60, 30, 7, 0])
    sent = state.setdefault("prep_reminded", {})
    due = []
    for i in items:
        if i["dni"] is None:
            continue
        hit = [t for t in thresholds if i["dni"] <= t]
        if not hit:
            continue
        t, key = min(hit), f"{i['id']}|{i['plati_do']}"
        if key in sent and sent[key] <= t:
            continue
        sent[key] = t
        due.append(i)
    keep = {f"{i['id']}|{i['plati_do']}" for i in items}
    for k in [k for k in sent if k not in keep]:
        del sent[k]
    if due:
        lines = [("⛔ " if i["dni"] < 0 else "📄 ") + prep_label(i) for i in due]
        lines.append("Vyřiď to teď v klidu – v krizi úřady nestíhají.")
        notify(state, cfg, "📄 Doklady a příprava na odjezd", "\n".join(lines),
               1 if any(i["dni"] <= 7 for i in due) else 0, cfg.get("dashboard_url", ""))


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
    return {"recommended": rec, "route_issues": route_issues(cfg, route, rec.get("trasa") if rec else None),
            "prep": prep_problems(state.get("prep_items", []))}


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
    prep = (ctx or {}).get("prep") or []
    if prep:
        lines.append("Než vyrazíš: " + "; ".join(prep))
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

    state["prep_items"] = preparedness(cfg, now)
    if do_notify:
        handle_notifications(cfg, state, events, overall, region_levels, now)
        remind_preparedness(cfg, state, state["prep_items"], now)

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
            msg = f"Stav doma: {oe} {on}. Funkčních zdrojů: {ok}/{len(state.get('sources', {}))}."
            todo = [i for i in state.get("prep_items", []) if i["stav"] in ("propadle", "nevyrizeno", "chybi_datum")]
            if todo:
                msg += f"\nPříprava – zbývá vyřídit {len(todo)}: " + "; ".join(i["nazev"] for i in todo[:5])
            notify(state, cfg, "✅ Hlídač běží", msg, 0, cfg.get("dashboard_url", ""))
            state["heartbeat_week"] = week

    for i in [i for i, t in notified.items() if now - (parse_date(t) or now) > timedelta(days=14)]:
        del notified[i]
    state["prev_active"] = list(active)
    state["prev_overall"] = overall


def east_overview(cfg, state, events) -> list:
    """Státní zdroje zemí východního křídla NATO: co se u které země hlídá, jestli to odpovídá a co hlásí."""
    zeme = (cfg.get("vychod") or {}).get("zeme") or []
    extras = state.get("extras", {})
    grid = {g["code"]: g for g in (extras.get("sit") or {}).get("site", [])}
    feeds = (state.get("sources", {}).get("news") or {}).get("feeds") or {}
    gz = extras.get("vestniky") or {}
    rad = (extras.get("radiace_hu") or {}).get("zona")
    src_ok = {k: v.get("ok", False) for k, v in state.get("sources", {}).items()}
    out = []
    for z in zeme:
        code, zdroje = z["code"], []
        g = grid.get(code)
        if g:
            info = (f"{g['actual'] / 1000:.1f} GW, {-g['dev']:+.0f} % proti " + ("normálu" if g["operator"] == "Transelectrica" else "předpovědi")
                    ).replace(".", ",") if "actual" in g else g.get("pozn", "")
            zdroje.append({"nazev": f"Elektrická síť ({g['operator']})", "info": info, "level": g.get("level", 0),
                           "ok": not str(g.get("pozn", "")).startswith("poslední")})   # „normál se sbírá“ není chyba
        if code == "PL" and gz.get("eli_pl"):
            zdroje.append({"nazev": "Dziennik Ustaw a Monitor Polski", "ok": src_ok.get("vestniky", False),
                           "info": f"{gz['eli_pl']['n']} aktů za týden"})
        if code == "LV" and gz.get("vestnesis_lv"):
            zdroje.append({"nazev": "Latvijas Vēstnesis", "ok": src_ok.get("vestniky", False),
                           "info": f"{gz['vestnesis_lv']['n']} aktů v posledním čísle"})
        if code == "HU" and rad:
            zdroje.append({"nazev": "Radiace (BM OKF)", "ok": src_ok.get("radiace_hu", False), "level": rad.get("level", 0),
                           "info": f"{rad['n']} stanic, nejvíc {rad['max']:.2f} µSv/h ({rad['max_name']})".replace(".", ",")})
        for f in cfg.get("news", {}).get("feeds", []):
            if f.get("zeme") == code:
                h = feeds.get(f["id"]) or {}
                zdroje.append({"nazev": f["name"], "ok": bool(h.get("ok")), "info": h.get("info") or h.get("error", "zatím bez dat")})
        evs = [e for e in events if e.get("zeme") == code]
        out.append({"code": code, "name": z.get("name", code), "pozn": z.get("pozn", ""),
                    "level": max([e["level"] for e in evs] or [0]), "events": evs[:4], "zdroje": zdroje})
    return out


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
    radiation = dict(extras.get("bfs") or {})
    hu = (extras.get("radiace_hu") or {}).get("zona")
    if hu:
        radiation["zones"] = list(radiation.get("zones") or []) + [hu]
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
        "radiation": radiation,
        "fuel": (extras.get("fuel") or {}).get("points", {}),
        "gas": extras.get("gas") or {},
        "evropa": europe_map(cfg, extras, regions, events),
        "lng": extras.get("lng") or {},
        "gasout": extras.get("gasout") or {},
        "internet": extras.get("ioda") or {},
        "gdelt": (extras.get("gdelt") or {}).get("dotazy", []),
        "quakes": extras.get("emsc") or {},
        "space": extras.get("noaa") or {},
        "vychod": east_overview(cfg, state, events),
        "utocista": {"sit": [g for g in (extras.get("sit") or {}).get("site", []) if g.get("region") != "EU"],
                     "vestniky": extras.get("vestniky") or {},
                     "anepc": extras.get("anepc") or {}, "seismo": extras.get("seismo_ib") or {}},
        "pripravenost": state.get("prep_items", []),
        "seznamy": cfg.get("seznamy") or DEFAULT_CHECKLISTS,
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
            "national": {"name": "Celé Německo", "n": 1688, "median": 0.081, "max": 0.192, "max_name": "Feldberg", "level": 0},
            "warn": (cfg.get("bfs_radiation") or {}).get("warn_usvh", 0.5),
            "alarm": (cfg.get("bfs_radiation") or {}).get("alarm_usvh", 2.0)},
        "gas": {"time": iso(now), "zeme": [
            {"code": "DE", "name": "Německo", "region": "DE", "day": "2026-11-10", "full": 58.4, "twh": 146.2, "net_gwh": 2210, "days": 66, "trend": -0.9, "level": 0},
            {"code": "CZ", "name": "Česko", "region": "CZ", "day": "2026-11-10", "full": 71.2, "twh": 31.0, "net_gwh": 310, "days": 100, "trend": -0.7, "level": 0},
            {"code": "EU", "name": "EU", "region": "EU", "day": "2026-11-10", "full": 69.9, "twh": 791.5, "net_gwh": 7100, "days": 111, "trend": -0.6, "level": 0}]},
        "lng": {"time": iso(now), "zeme": [{"code": "DE", "name": "Německo", "region": "DE", "day": "2026-11-11",
                                             "send": 412, "base": 455, "fill": 61, "cap": 900, "level": 0}]},
        "gasout": {"time": iso(now), "oblasti": [{"code": "DE", "name": "Německo", "gwh": 64, "n": 2, "level": 0},
                                                  {"code": "CZ", "name": "Česko", "gwh": 0, "n": 0, "level": 0}],
                   "items": [{"code": "DE", "kind": "plynovod", "name": "Waidhaus", "gwh": 48,
                              "to": iso(now + timedelta(hours=20)), "pozn": "Porucha kompresoru"},
                             {"code": "DE", "kind": "zásobník", "name": "UGS Etzel", "gwh": 16,
                              "to": iso(now + timedelta(days=2)), "pozn": ""}]},
        "ioda": {"time": iso(now), "zeme": [{"code": c, "name": n, "down": [], "level": 0}
                                             for c, n in (("CZ", "Česko"), ("DE", "Německo"), ("ES", "Španělsko"))]},
        "gdelt": {"dotazy": [{"id": "nato", "name": "Útok na NATO", "cur": 0.012, "base": 0.006, "ratio": 2.0, "time": iso(now)}]},
        "aa": {"zeme": {k: {"name": k, **v} for k, v in {
            "RU": {"warning": True}, "BY": {"warning": True}, "UA": {"warning": True}, "SY": {"warning": True},
            "LB": {"warning": True}, "IR": {"warning": True}, "IQ": {"warning": True}, "LY": {"warning": True},
            "MD": {"partialWarning": True}, "IL": {"partialWarning": True}, "TR": {"partialWarning": True},
            "AZ": {"partialWarning": True}, "AM": {"partialWarning": True}, "GE": {"partialWarning": True}}.items()}},
        "easa": {"czib": [{"title": "Airspace of the Russian Federation", "url": "https://www.easa.europa.eu/", "time": ""}]},
        "mapa": {"time": iso(now), "okno_hodin": 72, "zeme": {k: {"kat": kat, "zpravy": [
            {"kat": list(kat), "title": f"Ukázková zpráva ({k}): {', '.join(kat)}", "url": "https://example.org/",
             "zdroj": "Ukázka", "time": iso(now - timedelta(hours=5))}]} for k, kat in {
            "DK": {"drony": 4, "vzduch": 2}, "PL": {"drony": 2, "vzduch": 1}, "DE": {"drony": 1},
            "UA": {"utoky": 9, "drony": 6, "blackout": 2}, "RU": {"drony": 3, "utoky": 2}, "LT": {"vzduch": 1},
            "BE": {"drony": 2, "vzduch": 1}, "FR": {"utoky": 1}}.items()}},
        "noaa": {"now": 0, "max": 4, "day": "2026-11-14"},
        "emsc": {"days": 7, "min_mag": 2.0, "quakes": [
            {"zone": "Chebsko a Vogtland", "mag": 2.4, "time": iso(now - timedelta(days=2)), "place": "Czech Republic", "depth": 9}]},
        "sit": {"time": iso(now), "site": [
            {"code": "ES", "region": "ES", "name": "Španělsko", "operator": "REE", "level": 0, "actual": 27180, "forecast": 27420,
             "dev": 0.9, "time": iso(now - timedelta(minutes=10)), "stale": False},
            {"code": "IE", "region": "IE", "name": "Irsko", "operator": "EirGrid", "level": 0, "actual": 5040, "forecast": 5110,
             "dev": 1.4, "time": iso(now - timedelta(minutes=25)), "stale": False},
            {"code": "PL", "region": "EU", "name": "Polsko", "operator": "PSE", "level": 0, "actual": 18950, "forecast": 18700,
             "dev": -1.3, "time": iso(now - timedelta(minutes=20)), "stale": False},
            {"code": "EE", "region": "EU", "name": "Estonsko", "operator": "Elering", "level": 0, "actual": 1051, "forecast": 1040,
             "dev": -1.1, "time": iso(now - timedelta(minutes=20)), "stale": False},
            {"code": "RO", "region": "EU", "name": "Rumunsko", "operator": "Transelectrica", "level": 0, "pozn": "normál se teprve sbírá"}]},
        "vestniky": {"boe": {"n": 212, "dny": [(now - timedelta(days=1)).date().isoformat()]}, "oireachtas": {"n": 12},
                     "eli_pl": {"n": 24}, "vestnesis_lv": {"n": 17}, "zachyceno": []},
        "radiace_hu": {"zona": {"name": "Maďarsko (BM OKF)", "n": 80, "median": 0.112, "max": 0.181, "max_name": "Rétság",
                                "level": 0, "time": iso(now - timedelta(minutes=30))}},
        "anepc": {"n": 54, "lidi": 486, "velke": 1, "level": 1, "time": iso(now), "zasahy": [
            {"misto": "Arganil", "okres": "Coimbra", "druh": "Incêndio Rural em Povoamento Florestal", "lidi": 212,
             "letadla": 5, "stav": "Em Curso", "velky": True, "time": iso(now - timedelta(hours=3))}]},
        "seismo_ib": {"days": 7, "min_mag": 2.5, "quakes": [
            {"src": "IGN", "mag": 3.1, "time": iso(now - timedelta(hours=9)), "place": "Golfo De Cádiz", "region": "ES",
             "lat": 36.4, "lon": -7.6, "url": "https://www.ign.es/"}]},
    }
    ee = make_event("Zprávy", "EU", 1, "Estonsko – krizové varování (jen 1 zdroj – ověř): Ukázka – Eesti sulgeb piiripunktid "
                    "Venemaa piiril", url="https://www.kriis.ee/", eid="demo-ee", when=now - timedelta(hours=2))
    ee["zeme"] = "EE"
    events.append(ee)
    state["sources"]["news"]["feeds"] = {f["id"]: {"ok": True, "info": "20 titulků"} for f in cfg.get("news", {}).get("feeds", [])}
    events.append(make_event("ANEPC", "PT", 1, "Portugalsko – 1 velký zásah ANEPC, celkem 486 lidí v terénu: "
                             "Arganil (212 lidí, 5 letadel)", url="https://fogos.pt/", eid="anepc:L1", detail=ANEPC_ADVICE))
    state["prep_items"] = preparedness(cfg, now)
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
                public = ("noaa", "gas", "lng", "gasout", "ioda", "gdelt", "nukib", "psp", "easa", "mapa", "aa",
                          "sit", "vestniky", "anepc", "seismo_ib", "radiace_hu")   # info bez míst a tras
                print("::notice title=Signály::" + " · ".join(
                    f"{s['key']}: {s['info']}" for s in st["sources"] if s["key"] in public and s["ok"]), flush=True)
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
