"""Testy hlídače na vzorových datech (formáty ověřené proti živým zdrojům 11/2026).

Spuštění:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hlidac  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


def rss(items, ns=""):
    body = "".join(f"<item><title>{t}</title><link>https://x.example/{i}</link>"
                   f"<pubDate>{d or 'Thu, 12 Nov 2026 07:00:00 GMT'}</pubDate></item>" for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"{ns}><channel><title>x</title>{body}</channel></rss>'.encode()


GDACS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"><channel>
<item><title>Red flood alert in Czech Republic</title><link>http://www.gdacs.org/report.aspx?eventtype=FL&amp;eventid=1</link>
<pubDate>Tue, 10 Nov 2026 15:15:15 GMT</pubDate><guid isPermaLink="false">FL1</guid>
<gdacs:iscurrent>true</gdacs:iscurrent><gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:episodealertlevel>Red</gdacs:episodealertlevel>
<gdacs:iso3>CZE</gdacs:iso3><gdacs:country>Czech Republic</gdacs:country></item>
<item><title>Orange flood in Poland</title><link>http://g/2</link><guid>FL2</guid><gdacs:iscurrent>true</gdacs:iscurrent>
<gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:iso3>POL</gdacs:iso3></item>
<item><title>Green earthquake in Guatemala</title><link>http://g/3</link><guid>EQ3</guid><gdacs:iscurrent>true</gdacs:iscurrent>
<gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:iso3>GTM</gdacs:iso3></item>
</channel></rss>"""

METEO_CZ = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
<entry><cap:areaDesc>Hlavn\xc3\xad m\xc4\x9bsto Praha</cap:areaDesc><cap:event>Extreme Impact of Flooding</cap:event>
<cap:severity>Extreme</cap:severity><cap:onset>2026-11-12T02:00:00+00:00</cap:onset><cap:expires>2026-11-13T08:59:59+00:00</cap:expires>
<cap:message_type>Alert</cap:message_type><cap:identifier>ID1</cap:identifier></entry>
<entry><cap:areaDesc>Hlavn\xc3\xad m\xc4\x9bsto Praha</cap:areaDesc><cap:event>Extreme Impact of Flooding</cap:event>
<cap:severity>Extreme</cap:severity><cap:expires>2026-11-13T08:59:59+00:00</cap:expires>
<cap:message_type>Alert</cap:message_type><cap:identifier>ID1</cap:identifier></entry>
<entry><cap:areaDesc>Kraj Vyso\xc4\x8dina (Telc)</cap:areaDesc><cap:event>Extreme Impact of Snow</cap:event>
<cap:severity>Extreme</cap:severity><cap:expires>2026-11-13T08:59:59+00:00</cap:expires><cap:message_type>Alert</cap:message_type><cap:identifier>ID2</cap:identifier></entry>
<entry><cap:areaDesc>Hlavn\xc3\xad m\xc4\x9bsto Praha</cap:areaDesc><cap:event>Severe Impact of Wind</cap:event>
<cap:severity>Severe</cap:severity><cap:expires>2026-11-01T08:59:59+00:00</cap:expires><cap:message_type>Alert</cap:message_type><cap:identifier>OLD</cap:identifier></entry>
</feed>"""

METEO_ES = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2"></feed>"""

AA = json.dumps({"response": {
    "lastModified": 1787576330, "contentList": ["210456", "210534", "1"],
    "210456": {"countryCode": "CZ", "countryName": "Tschechische Republik", "warning": True, "partialWarning": False,
               "situationWarning": False, "situationPartWarning": False},
    "210534": {"countryCode": "ES", "countryName": "Spanien", "warning": False, "partialWarning": False,
               "situationWarning": False, "situationPartWarning": False},
    "1": {"countryCode": "PL", "countryName": "Polen", "warning": False, "partialWarning": True,
          "situationWarning": False, "situationPartWarning": False}}}).encode()

US = b"""<?xml version="1.0" encoding="UTF-8"?><rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
<item><title>Iran - Level 4: Do Not Travel</title><link>https://t/irn</link><category domain="Country-Tag">IR</category></item>
<item><title>Czechia - Level 4: Do Not Travel</title><link>https://t/cze</link><category domain="Country-Tag">EZ</category></item>
<item><title>Germany - Level 2: Exercise Increased Caution</title><link>https://t/deu</link></item>
<item><title>Poland - Level 3: Reconsider Travel</title><link>https://t/pol</link></item>
</channel></rss>"""

NOAA = json.dumps({
    "-1": {"DateStamp": "2026-11-11", "G": {"Scale": "1", "Text": "minor"}},
    "0": {"DateStamp": "2026-11-12", "TimeStamp": "00:17:00", "G": {"Scale": "0", "Text": "none"}},
    "1": {"DateStamp": "2026-11-12", "G": {"Scale": None, "Text": None}},
    "2": {"DateStamp": "2026-11-13", "G": {"Scale": "5", "Text": "extreme"}},
    "3": {"DateStamp": "2026-11-14", "G": {"Scale": "2", "Text": "moderate"}}}).encode()


def bfs(values_near, values_far=()):
    feats = []
    for i, v in enumerate(values_near):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [12.9, 50.4]},
                      "properties": {"name": f"Hranice{i}", "value": v, "site_status": 1}})
    for i, v in enumerate(values_far):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [9.29, 53.82]},
                      "properties": {"name": f"Sever{i}", "value": v, "site_status": 1}})
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


NINA_LEIPZIG = json.dumps([
    {"id": "mow.DE-SN-L-001", "payload": {"data": {"headline": "Gefahreninformation: Großbrand", "severity": "Extreme",
                                                   "provider": "MOWAS", "msgType": "Alert"}}, "sent": "2026-11-12T06:00:00+01:00"},
    {"id": "dwd.2.49.0.0.276.0.DWD.PVW.1", "payload": {"data": {"headline": "Amtliche Warnung vor STURMBÖEN", "severity": "Severe",
                                                                "msgType": "Alert"}}},
    {"id": "mow.DE-SN-L-002", "payload": {"data": {"headline": "Entwarnung", "severity": "Extreme", "msgType": "Cancel"}}},
]).encode()

NINA_MAG = json.dumps([
    {"id": "mow.DE-ST-MD-777", "payload": {"data": {"headline": "Warnung vor Luftangriff", "severity": "Extreme", "msgType": "Alert"}}},
]).encode()

MOWAS_NATIONAL = json.dumps([
    {"id": "mow.DE-SL-SLS-W038", "version": 19, "startDate": "2026-09-04T13:05:28+02:00", "severity": "Minor",
     "type": "Update", "i18nTitle": {"de": "Trinkwasser abkochen"}},
    {"id": "mow.DE-BY-X", "severity": "Extreme", "type": "Alert", "i18nTitle": {"de": "Chemieunfall in Ingolstadt"}},
]).encode()


def chmi_meta(stations):
    header = ("objID, DBC, STATION_NAME, STREAM_NAME, GEOGR1, GEOGR2, SPA_TYP, SPAH_DS, SPAH_UNIT, DRYH, SPA1H, SPA2H, "
              "SPA3H, SPA4H, SPAQ_DS, SPAQ_UNIT, DRYQ, SPA1Q, SPA2Q, SPA3Q, SPA4Q, ISFORECAST")
    rows = [[sid, "x", name, river, 50.3, 13.8, "H", "", "CM", None, l1, l2, l3, None, "", "M3_S", None, 1, 2, 3, None, 1]
            for sid, name, river, (l1, l2, l3) in stations]
    return json.dumps({"zaznamID": "x", "data": {"type": "DataCollection", "data": {"header": header, "values": rows}}}).encode()


def chmi_data(sid, h_series, hf_series=()):
    ts = lambda vals: [{"dt": d, "value": v} for d, v in vals]
    return json.dumps({"objList": [{"objID": sid, "tsList": [
        {"tsConID": "H", "unit": "CM", "tsData": ts(h_series)},
        {"tsConID": "H_F", "unit": "CM", "tsData": ts(hf_series)},
        {"tsConID": "Q", "unit": "M3_S", "tsData": [{"dt": "2026-11-12T07:50:00Z", "value": 10.5}]}]}]}).encode()


CHMI_META = chmi_meta([("0-203-1-219000", "Louny", "Ohře", (400, 440, 490))])
CHMI_CALM = chmi_data("0-203-1-219000", [("2026-11-12T07:00:00Z", 180), ("2026-11-12T07:50:00Z", 182)])

AUTOBAHN_CLOSURE = json.dumps({"closure": [
    {"identifier": "a", "isBlocked": "true", "future": False, "display_type": "CLOSURE",
     "title": "A6 | Schwetzingen/Hockenheim - Walldorf", "subtitle": " Mannheim -> Heilbronn"},
    {"identifier": "b", "isBlocked": "true", "future": True, "display_type": "CLOSURE", "title": "A6 | budoucí", "subtitle": ""},
    {"identifier": "c", "isBlocked": "false", "future": False, "display_type": "CLOSURE", "title": "A6 | jen pruh", "subtitle": ""}]}).encode()
AUTOBAHN_WARNING = json.dumps({"warning": [
    {"identifier": "w1", "display_type": "WARNING", "title": "A6 | Kreuz Nürnberg-Süd", "subtitle": "Heilbronn -> Nürnberg",
     "delayTimeValue": "75", "abnormalTrafficType": "STATIONARY_TRAFFIC", "isBlocked": "false"},
    {"identifier": "w2", "display_type": "WARNING", "title": "A6 | malá kolona", "subtitle": "", "delayTimeValue": "10",
     "isBlocked": "false"}]}).encode()
EMPTY_CLOSURE, EMPTY_WARNING = b'{"closure": []}', b'{"warning": []}'


class FakeNet:
    def __init__(self, routes):
        self.routes = dict(routes)

    def __call__(self, url, timeout=25):
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val() if callable(val) else val
        if "warnung.bund.de/api31/dashboard/" in url:
            return b"[]"
        raise OSError(f"no route for {url}")


def base_routes():
    return {
        "dashboard/146120000000": NINA_LEIPZIG,
        "mowas/mapData.json": b"[]",
        "noaa-scales.json": json.dumps({"0": {"G": {"Scale": "0"}}}).encode(),
        "gdacs.org": rss([]),
        "atom-czechia": METEO_ES,
        "atom-spain": METEO_ES,
        "atom-portugal": METEO_ES,
        "atom-ireland": METEO_ES,
        "chmi.cz/hydrology/now/metadata": CHMI_META,
        "chmi.cz/hydrology/now/data": CHMI_CALM,
        "/services/closure": EMPTY_CLOSURE,
        "/services/warning": EMPTY_WARNING,
        "rtp.pt": rss([]), "rte.ie": rss([]),
        "auswaertiges-amt": json.dumps({"response": {}}).encode(),
        "travel.state.gov": US.replace(b"Czechia - Level 4", b"Czechia - Level 1").replace(b"Poland - Level 3", b"Poland - Level 1"),
        "imis.bfs.de": bfs([0.09, 0.1, 0.11]),
        "ct24": rss([("Počasí: o víkendu sníh", None)]),
        "irozhlas": rss([]), "novinky": rss([]), "tagesschau": rss([]), "mdr": rss([]), "elpais": rss([]), "bbc": rss([]),
    }


class TestNorm(unittest.TestCase):
    def test_diacritics(self):
        self.assertEqual(hlidac.norm("Vláda NAVRHLA stav ohrožení státu"), "vlada navrhla stav ohrozeni statu")
        self.assertEqual(hlidac.norm("Großflächiger Stromausfall in Thüringen"), "grossflachiger stromausfall in thuringen")
        self.assertEqual(hlidac.norm("Gran apagón en toda España"), "gran apagon en toda espana")


class TestRules(unittest.TestCase):
    rules = hlidac.compile_rules(CFG)

    def ids(self, title):
        return {r["id"] for r in hlidac.match_rules(title, self.rules)}

    def test_positive(self):
        cases = {
            "Vláda navrhla Sněmovně vyhlásit stav ohrožení státu": "stav_ohrozeni",
            "Sněmovna schválila stav ohrožení státu pro celé Česko": "stav_ohrozeni",
            "Prezident vyhlásil válečný stav": "stav_ohrozeni",
            "Armáda ČR zahájila mobilizaci záloh": "mobilizace_cz",
            "Tisíce záložníků dostaly povolávací rozkaz": "mobilizace_cz",
            "Bundestag stellt Spannungsfall fest": "valka_de",
            "Luftalarm in Dresden: Bevölkerung soll Schutz suchen": "valka_de",
            "NATO aktivovalo článek 5 po útoku na Litvu": "nato_clanek5",
            "Bündnisfall ausgerufen": "nato_clanek5",
            "Allies invoke Article 5 after strike on Poland": "nato_clanek5",
            "Rusko zaútočilo na Polsko, drony zasáhly sklad": "utok_na_nato",
            "Estland angegriffen: Raketen schlagen in Narva ein": "utok_na_nato",
            "Jaderný výbuch u Kaliningradu": "jaderny",
            "Únik radiace ze Záporožské elektrárny": "radiace",
            "ČEPS vyhlásila stav nouze v elektroenergetice": "blackout_cz",
            "Rozsáhlý výpadek proudu zasáhl statisíce domácností": "blackout_cz",
            "Großflächiger Stromausfall in Sachsen": "blackout_de",
            "Bundesnetzagentur ruft Alarmstufe Gas aus": "blackout_de",
            "Gran apagón en toda España": "spanelsko",
            "Polsko uzavřelo hranice s Běloruskem": "hranice",
            "USA evakuují ambasádu v Praze": "ambasady",
            "US orders departure of embassy staff in Warsaw": "ambasady",
        }
        for title, rid in cases.items():
            with self.subTest(title=title):
                self.assertIn(rid, self.ids(title))

    def test_negative(self):
        cases = [
            "Rusko vyhlásilo částečnou mobilizaci",
            "Luftalarm in Kiew: Russland greift erneut an",
            "Putin hrozí jaderným úderem, varují experti",
            "Jak se připravit na blackout? Rady hasičů",
            "Blackout ve Španělsku: rok poté",
            "Izrael uzavřel vzdušný prostor",
            "Kyberútok na polské nemocnice",
            "Fotbalisté Slavie porazili Plzeň",
            "Russland testet Atomwaffe mit neuer Rakete",
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(self.ids(title) - {"spanelsko"} if "Španěl" in title else self.ids(title), set())


class TestSources(unittest.TestCase):
    def test_nina(self):
        cfg = {**CFG, "nina": {"areas": [{"ars": "146120000000", "name": "Dresden"}, {"ars": "095640000000", "name": "Nürnberg"}]}}
        ev, info = hlidac.src_nina(cfg, FakeNet({"146120000000": NINA_LEIPZIG, "095640000000": NINA_MAG}))
        by = {e["id"]: e for e in ev}
        self.assertEqual(by["nina:mow.DE-SN-L-001"]["level"], 2)
        self.assertEqual(by["nina:mow.DE-ST-MD-777"]["level"], 3)
        self.assertNotIn("nina:mow.DE-SN-L-002", by)          # Cancel
        self.assertFalse(any(i.startswith("nina:dwd") for i in by))  # DWD Severe je běžné počasí
        self.assertIn("Dresden", by["nina:mow.DE-SN-L-001"]["title"])

    def test_nina_national(self):
        ev, _ = hlidac.src_nina_national(CFG, FakeNet({"mapData": MOWAS_NATIONAL}))
        self.assertEqual([(e["id"], e["level"]) for e in ev], [("nina:mow.DE-BY-X", 1)])

    def test_noaa(self):
        ev, info = hlidac.src_noaa(CFG, FakeNet({"noaa": NOAA}))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["region"], ev[0]["level"]), ("EU", 2))
        self.assertIn("G5", ev[0]["title"])
        self.assertEqual(info, "teď G0, předpověď max G5")

    def test_gdacs(self):
        ev, _ = hlidac.src_gdacs(CFG, FakeNet({"gdacs": GDACS}))
        self.assertEqual([(e["region"], e["level"]) for e in ev], [("CZ", 2)])

    def test_meteoalarm(self):
        ev, info = hlidac.src_meteoalarm(CFG, FakeNet({"atom-czechia": METEO_CZ, "atom-": METEO_ES}))
        self.assertEqual(len(ev), 1)                            # duplicita, Vysočina i prošlá výstraha vyřazeny
        self.assertEqual((ev[0]["region"], ev[0]["level"]), ("CZ", 2))

    def test_aa(self):
        ev, _ = hlidac.src_auswaertiges_amt(CFG, FakeNet({"auswaertiges": AA}))
        got = sorted((e["region"], e["level"]) for e in ev)
        self.assertEqual(got, [("CZ", 3), ("EU", 1)])

    def test_us_state(self):
        ev, info = hlidac.src_us_state(CFG, FakeNet({"travel.state.gov": US}))
        got = sorted((e["region"], e["level"]) for e in ev)
        self.assertEqual(got, [("CZ", 3), ("EU", 1)])          # Íránu si nevšímá, Německo L2 je běžné
        self.assertIn("Germany L2", info)

    def test_bfs(self):
        ev, _ = hlidac.src_bfs(CFG, FakeNet({"imis.bfs.de": bfs([0.8, 0.7, 0.65, 0.1], [5.0] * 2)}))
        self.assertEqual([(e["region"], e["level"]) for e in ev], [("CZ", 2)])
        ev, _ = hlidac.src_bfs(CFG, FakeNet({"imis.bfs.de": bfs([0.1, 0.12, 0.3, 0.09])}))
        self.assertEqual(ev, [])                                 # běžný déšť / pozadí
        ev, _ = hlidac.src_bfs(CFG, FakeNet({"imis.bfs.de": bfs([2.5, 3.1, 2.2])}))
        self.assertEqual(ev[0]["level"], 3)


class TestNewSources(unittest.TestCase):
    def test_chmi_stages_forecast_rise(self):
        meta = chmi_meta([("A", "Žatec", "Ohře", (115, 140, 180)), ("B", "Louny", "Ohře", (190, 230, 270)),
                          ("C", "Terezín", "Ohře", (260, 290, None))])
        cfg = {**CFG, "chmi_hydro": {**CFG["chmi_hydro"], "stations": [
            {"id": "A", "name": "Žatec – Ohře"}, {"id": "B", "name": "Louny – Ohře"}, {"id": "C", "name": "Terezín – Ohře"}]}}
        now = datetime.now(timezone.utc)
        t = lambda m: (now + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:00Z")
        net = FakeNet({
            "metadata": meta,
            "data/A.json": chmi_data("A", [(t(-60), 150), (t(-10), 185)]),                         # 3. SPA → 2
            "data/B.json": chmi_data("B", [(t(-60), 150), (t(-10), 160)], [(t(600), 235)]),       # předpověď 2. SPA → 1
            "data/C.json": chmi_data("C", [(t(-170), 100), (t(-10), 150)]),                        # +50 cm za 3 h → 1
        })
        mem = {}
        ev, info, extra = hlidac.src_chmi(cfg, net, mem)
        got = sorted((e["title"].split(":")[0], e["level"]) for e in ev)
        self.assertEqual(got, [("Louny – Ohře", 1), ("Terezín – Ohře", 1), ("Žatec – Ohře", 2)])
        self.assertIn("předpověď 2. SPA", next(e["title"] for e in ev if e["title"].startswith("Louny")))
        self.assertEqual([r["stage"] for r in extra["rivers"]], [3, 0, 0])
        self.assertIn("meta", mem)                                     # metadata se drží 24 h

    def test_route(self):
        cfg = {**CFG, "plan": {"trasy": [{"id": "x", "dalnice": ["A6", "A5"]}]}}
        net = FakeNet({"A6/services/closure": AUTOBAHN_CLOSURE, "A6/services/warning": AUTOBAHN_WARNING,
                       "A5/services/closure": EMPTY_CLOSURE, "A5/services/warning": EMPTY_WARNING})
        ev, info, extra = hlidac.src_route(cfg, net)
        a6 = extra["roads"]["A6"]
        self.assertEqual(ev, [])                                        # trasa sama stupeň nezvedá
        self.assertEqual([c["title"] for c in a6["closures"]], ["A6 | Schwetzingen/Hockenheim - Walldorf", "A6 | jen pruh"])
        self.assertEqual([j["delay"] for j in a6["jams"]], [75])
        self.assertTrue(a6["problem"])
        self.assertFalse(extra["roads"]["A5"]["problem"])
        lines = hlidac.route_issues(cfg, extra, "x")
        self.assertEqual(lines, ["A6 | Schwetzingen/Hockenheim - Walldorf uzavřeno (Mannheim -> Heilbronn)",
                                 "A6 | Kreuz Nürnberg-Süd: zdržení 75 min (Heilbronn -> Nürnberg)"])

    def test_fuel(self):
        cfg = {**CFG, "fuel": {**CFG["fuel"], "points": [{"name": "N", "lat": 49.4, "lon": 11.0}]}}
        self.assertIn("vypnuto", hlidac.src_fuel(cfg, FakeNet({}), {})[1])          # bez klíče nic nedělá
        os.environ["TANKERKOENIG_KEY"] = "k"
        try:
            def resp(n_open, n_total, price):
                st = [{"id": str(i), "isOpen": i < n_open, "diesel": price} for i in range(n_total)]
                return json.dumps({"ok": True, "stations": st}).encode()
            mem = {}
            for _ in range(25):
                hlidac.src_fuel(cfg, FakeNet({"tankerkoenig": resp(20, 22, 1.60)}), mem)
            ev, info, extra = hlidac.src_fuel(cfg, FakeNet({"tankerkoenig": resp(20, 22, 2.10)}), mem)
            self.assertEqual([e["eid"] if "eid" in e else e["id"] for e in ev], ["fuel:N:price"])
            ev, *_ = hlidac.src_fuel(cfg, FakeNet({"tankerkoenig": resp(3, 22, 1.60)}), mem)
            hour = datetime.now(timezone.utc).astimezone(hlidac.local_tz(cfg)).hour
            if 7 <= hour <= 20:
                self.assertIn("fuel:N:closed", [e["id"] for e in ev])
        finally:
            os.environ.pop("TANKERKOENIG_KEY")

    def test_rule_feed_filter(self):
        rules = hlidac.compile_rules(CFG)
        ids = {r["id"] for r in hlidac.match_rules("Apagão geral deixa país às escuras", rules)}
        self.assertIn("portugalsko_rtp", ids)
        rule = next(r for r in rules if r["id"] == "portugalsko_rtp")
        self.assertEqual(rule["feeds"], ["rtp"])
        self.assertNotIn("spanelsko", {r["id"] for r in hlidac.match_rules("Estado de excepción en Ecuador", rules)})


class TestRefuges(unittest.TestCase):
    def test_recommendation(self):
        rec = hlidac.recommend_refuge(CFG, {"ES": 2, "PT": 0, "IE": 0})
        self.assertEqual((rec["code"], rec["calm"]), ("PT", True))            # nejklidnější, pak pořadí
        rec = hlidac.recommend_refuge(CFG, {"ES": 0, "PT": 0, "IE": 0})
        self.assertEqual(rec["code"], "ES")                                    # preference
        rec = hlidac.recommend_refuge(CFG, {"ES": 3, "PT": 2, "IE": 2})
        self.assertEqual((rec["code"], rec["calm"]), ("PT", False))
        self.assertEqual(hlidac.region_order(CFG), ["CZ", "DE", "EU", "ES", "PT", "IE"])


class TestCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hlidac.DATA = Path(self.tmp.name)
        self.sent = []
        self._orig = hlidac.send_notification
        hlidac.send_notification = lambda title, msg, level, url="": (self.sent.append((title, msg, level)) or ["test"])
        for k in ("HEALTHCHECK_URL",):
            os.environ.pop(k, None)

    def tearDown(self):
        hlidac.send_notification = self._orig
        self.tmp.cleanup()

    def test_flow(self):
        state = {}
        routes = base_routes()
        # 1) první spuštění: jedno souhrnné upozornění
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("spuštěn", self.sent[0][0])
        self.assertEqual(st["overall"]["level"], 2)            # Großbrand Extreme v Drážďanech → DE 2
        self.assertTrue(all(s["ok"] for s in st["sources"]), [s for s in st["sources"] if not s["ok"]])

        # 2) beze změny: nic nového
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=10))
        self.assertEqual(len(self.sent), 1)

        # 3) jeden zdroj zpráv hlásí stav ohrožení → stupeň 2 „ověř“
        routes["ct24"] = rss([("Vláda navrhla vyhlásit stav ohrožení státu", None)])
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=20))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][2], 2)
        self.assertIn("ověř", self.sent[-1][1])

        # 4) druhý nezávislý zdroj → potvrzeno → stupeň 3 a poznámka o souhlasu k vycestování
        routes["irozhlas"] = rss([("Sněmovna dnes projedná návrh vlády vyhlásit stav ohrožení státu", None)])
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=30))
        self.assertEqual(self.sent[-1][2], 3)
        self.assertEqual(st["overall"]["level"], 3)
        cz = next(r for r in st["regions"] if r["code"] == "CZ")
        self.assertEqual(cz["level"], 3)
        self.assertIn("souhlas", cz["events"][0]["detail"])

        # 5) výpadek zdroje NINA: poslední známý stav se drží, nic nového se neposílá
        n = len(self.sent)
        routes["dashboard/146120000000"] = OSError("timeout")
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=40))
        de = next(r for r in st["regions"] if r["code"] == "DE")
        self.assertEqual(de["level"], 2)
        self.assertEqual(len(self.sent), n)
        nina = next(s for s in st["sources"] if s["key"] == "nina")
        self.assertFalse(nina["ok"])

        # 6) po 7 h nefunkčnosti: jedno upozornění na slepé místo, stará data už se nedrží
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(hours=7, minutes=40))
        self.assertTrue(any("Zdroj nefunguje" in t for t, _, _ in self.sent))
        de = next(r for r in st["regions"] if r["code"] == "DE")
        self.assertEqual(de["level"], 0)

        # 7) zprávy vyprší po hold_hours → zlepšení
        routes["ct24"] = rss([])
        routes["irozhlas"] = rss([])
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(hours=13))
        self.assertEqual(st["overall"]["level"], 0)
        self.assertIn("Zlepšení", self.sent[-1][0])

        # status.json je validní a obsahuje vše pro dashboard
        status = json.loads((hlidac.DATA / "status.json").read_text(encoding="utf-8"))
        for key in ("overall", "regions", "events", "sources", "actions", "plan", "history"):
            self.assertIn(key, status)

    def test_level3_reminders(self):
        routes = base_routes()
        routes["dashboard/146120000000"] = b"[]"
        state = {}
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW)               # klid, start
        routes["ct24"] = rss([("Vláda navrhla vyhlásit stav ohrožení státu", None)])
        routes["irozhlas"] = rss([("Sněmovna projedná návrh vyhlásit stav ohrožení státu", None)])
        routes["/services/closure"] = AUTOBAHN_CLOSURE
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=10))
        title, msg, lvl = self.sent[-1]
        self.assertEqual(lvl, 3)
        self.assertIn("Doporučený cíl: Španělsko", msg)
        self.assertIn("Na trase: A6 | Schwetzingen", msg)
        n = len(self.sent)
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=20))
        self.assertEqual(len(self.sent), n)                                         # za 10 min ještě ne
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=40))
        self.assertEqual(len(self.sent), n + 1)
        self.assertEqual(self.sent[-1][0], "🔴 Stále ODJEĎ")
        self.assertEqual(self.sent[-1][2], 3)
        hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW + timedelta(minutes=50))
        self.assertEqual(len(self.sent), n + 1)

    def test_old_news_ignored(self):
        routes = base_routes()
        routes["ct24"] = rss([("Vláda navrhla vyhlásit stav ohrožení státu", "Mon, 02 Nov 2026 07:00:00 GMT")])
        state = {}
        st = hlidac.run_cycle(CFG, state, fetch=FakeNet(routes), now=NOW)
        cz = next(r for r in st["regions"] if r["code"] == "CZ")
        self.assertEqual(cz["level"], 0)

    def test_spain_not_in_overall(self):
        routes = base_routes()
        routes["dashboard/146120000000"] = b"[]"
        routes["elpais"] = rss([("Gran apagón en toda España", None)])
        routes["bbc"] = rss([("Spain blackout leaves millions without power", None)])
        st = hlidac.run_cycle(CFG, {}, fetch=FakeNet(routes), now=NOW)
        es = next(r for r in st["regions"] if r["code"] == "ES")
        self.assertEqual(es["level"], 2)
        self.assertEqual(st["overall"]["level"], 0)            # potíže v útočišti ≠ útěk z domova


class TestSite(unittest.TestCase):
    def test_encrypted_site(self):
        import base64
        import hashlib
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        tmp = tempfile.TemporaryDirectory()
        site = Path(tmp.name) / "site"
        os.environ["DASH_TOKEN"] = "kod-123"
        try:
            hlidac.build_site(site, {"overall": {"level": 2}, "plan": {"trasa": "Domov → Cíl"}})
        finally:
            os.environ.pop("DASH_TOKEN")
        self.assertFalse((site / "status.json").exists())           # nic nešifrovaného
        self.assertTrue((site / "index.html").exists() and (site / ".nojekyll").exists())
        env = json.loads((site / "status.enc.json").read_text())
        self.assertNotIn("Domov", json.dumps(env))
        key = hashlib.pbkdf2_hmac("sha256", b"kod-123", base64.b64decode(env["salt"]), env["iter"], dklen=32)
        pt = AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None)
        self.assertEqual(json.loads(pt)["plan"]["trasa"], "Domov → Cíl")
        tmp.cleanup()

    def test_relative_paths(self):
        web = Path(hlidac.WEB)
        html_src = (web / "index.html").read_text(encoding="utf-8")
        for bad in ('href="/', 'src="/', "url(/", 'fetch("/', 'register("/'):
            self.assertNotIn(bad, html_src)                               # web musí fungovat i na /repo/


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.saved = (hlidac.CONFIG_PATH, hlidac.ENC_CONFIG_PATH, dict(os.environ))
        hlidac.CONFIG_PATH = d / "config.json"
        hlidac.ENC_CONFIG_PATH = d / "config.enc.json"
        hlidac.CONFIG_PATH.write_text(json.dumps({"plan": {"cil": "ukázka"}}), encoding="utf-8")
        hlidac.ENC_CONFIG_PATH.write_text(json.dumps(hlidac.encrypt_status(
            {"plan": {"cil": "osobní"}, "env": {"PUSHOVER_USER": "u123"}}, "kod-1")), encoding="utf-8")
        for k in ("CONFIG_JSON", "DASH_TOKEN", "PUSHOVER_USER", "HLIDAC_CONFIG", "DASHBOARD_URL"):
            os.environ.pop(k, None)

    def tearDown(self):
        hlidac.CONFIG_PATH, hlidac.ENC_CONFIG_PATH, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def test_priority(self):
        self.assertEqual(hlidac.load_config()["plan"]["cil"], "ukázka")          # bez kódu → ukázka
        os.environ["DASH_TOKEN"] = "spatny"
        self.assertEqual(hlidac.load_config()["plan"]["cil"], "ukázka")          # špatný kód → ukázka
        os.environ["DASH_TOKEN"] = "kod-1"
        os.environ["PUSHOVER_USER"] = ""                                          # prázdný secret z Actions
        self.assertEqual(hlidac.load_config()["plan"]["cil"], "osobní")
        self.assertEqual(os.environ["PUSHOVER_USER"], "u123")
        os.environ["CONFIG_JSON"] = json.dumps({"plan": {"cil": "secret"}})
        self.assertEqual(hlidac.load_config()["plan"]["cil"], "secret")          # secret má přednost


class TestNotify(unittest.TestCase):
    def test_ntfy_defaults_and_no_token(self):
        saved_env, saved_post = dict(os.environ), hlidac.http_post
        calls = []
        hlidac.http_post = lambda url, form=None, js=None, timeout=20: calls.append((url, js)) or b"{}"
        try:
            for k in ("PUSHOVER_TOKEN", "PUSHOVER_USER", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
                os.environ.pop(k, None)
            os.environ.update(NTFY_TOPIC="tajne-tema", NTFY_SERVER="")      # prázdný secret z Actions
            sent = hlidac.send_notification("🔴 ODJEĎ", "zpráva", 3, "https://x.github.io/krizovy-hlidac/?k=TAJNE")
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            hlidac.http_post = saved_post
        self.assertEqual(sent, ["ntfy"])
        url, body = calls[0]
        self.assertEqual(url, "https://ntfy.sh/")
        self.assertEqual((body["topic"], body["priority"]), ("tajne-tema", 5))
        self.assertEqual(body["click"], "https://x.github.io/krizovy-hlidac/")
        self.assertNotIn("TAJNE", json.dumps(body))


class TestServer(unittest.TestCase):
    def test_token_and_paths(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        tmp = tempfile.TemporaryDirectory()
        hlidac.DATA = Path(tmp.name)
        (hlidac.DATA / "status.json").write_text('{"ok":1}', encoding="utf-8")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hlidac.make_handler("tajne123"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path, headers=None):
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {})) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read(), dict(e.headers)

        self.assertEqual(get("/status.json")[0], 401)
        self.assertEqual(get("/status.json?k=spatne")[0], 401)
        code, body, hdr = get("/status.json?k=tajne123")
        self.assertEqual((code, body), (200, b'{"ok":1}'))
        self.assertIn("hk=tajne123", hdr.get("Set-Cookie", ""))
        self.assertEqual(get("/status.json", {"Cookie": "hk=tajne123"})[0], 200)
        code, body, _ = get("/manifest.webmanifest?k=tajne123")
        self.assertEqual(json.loads(body)["start_url"], "./?k=tajne123")
        self.assertEqual(get("/%2e%2e/config.json?k=tajne123")[0], 404)
        self.assertEqual(get("/health")[0], 200)
        self.assertEqual(get("/?k=%C5%99")[0], 401)              # ne-ASCII token nespadne
        srv.shutdown()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
