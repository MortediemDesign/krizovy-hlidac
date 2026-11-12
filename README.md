# Krizový hlídač

Osobní systém včasného varování před válkou, rozsáhlým blackoutem a katastrofami. Každých 10 minut projde oficiální zdroje i zprávy a ohodnotí situaci stupněm 0–3 pro domov, práci, Evropu a zvolené útočiště. Když se něco zhorší, pošle upozornění na mobil. Webový dashboard (aplikace na plochu telefonu) má čtyři záložky: **Stav** (stupeň, oblasti, přehled ukazatelů, co se děje), **Plán** (mapa Evropy s trasami a útočišti, trasy do offline map), **Signály** (vodočty, radiace, plyn, internet, kosmické počasí, zemětřesení, zdroje) a **Příprava** (platnost dokladů, úkoly před cestou, seznamy na 72 hodin).

| Stupeň | Význam | Typický spouštěč |
|---|---|---|
| 🟢 Klid | nic neobvyklého | — |
| 🟡 Sleduj | zvýšené napětí | výstraha ČHMÚ, 2. SPA na vodočtu, geomagnetická bouře G4, zavřené pumpy na trase, jednotlivá zpráva o výpadku nebo útoku, plyn v zásobnících pod 20 %, kybernetické varování NÚKIB, prudký nárůst světových zpráv, otřes od M3,5 v okolí |
| 🟠 Připrav se | hrozba v řádu dnů | výstraha NINA „Extreme“, 3. SPA na vodočtu, zvýšená radiace u hranic, zpráva o stavu ohrožení státu z 1 zdroje, předpověď bouře G5, výpadek internetu celé země, vládní návrh stavu ohrožení státu ve Sněmovně, bulletin EASA pro vzdušný prostor v Evropě |
| 🔴 Odjeď | bezprostřední hrozba | letecký poplach v Německu, potvrzený návrh stavu ohrožení státu, cestovní varování pro ČR, čl. 5 NATO, bulletin EASA pro vzdušný prostor Česka nebo Německa |

Celkový stav se počítá z domova, práce a Evropy. Útočiště (v ukázce Španělsko, Portugalsko a Irsko) se hodnotí zvlášť: problém tam znamená „jeď jinam“, ne „uteč z domova“. Dashboard i upozornění od stupně 🟠 doporučí nejklidnější útočiště a ukážou komplikace na trase k němu. Dokud trvá 🔴, hlídač upozornění opakuje každých 30 minut.

**Proč spěchat u stavu ohrožení státu:** za stavu ohrožení státu nebo válečného stavu potřebuje člověk s brannou povinností k cestě do ciziny souhlas krajského vojenského velitelství (branný zákon č. 585/2004 Sb.) a na souhlas není nárok. Proto už potvrzená zpráva o *návrhu* vlády znamená 🔴.

---

## Dva způsoby provozu

**A) GitHub, zdarma a bez serveru.** GitHub Actions spouští hlídač každých 10 minut a web běží na GitHub Pages. Osobní konfigurace je v šifrovaném secretu a stav na webu je zašifrovaný kódem, takže veřejně čitelný není ani ve veřejném repu. Nevýhoda: GitHub plánované běhy občas o pár minut zpozdí.

**B) Vlastní server (VPS, Raspberry Pi, hosting).** Hlídač běží nepřetržitě a dashboard obsluhuje sám. Server mimo domov přežije výpadek proudu u tebe.

### A) GitHub

1. **Pages:** Settings → Pages → Source: *GitHub Actions*.
2. **Secret** (Settings → Secrets and variables → Actions → New repository secret): `DASH_TOKEN` – kód k webu, dlouhý náhodný řetězec (`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`). Bez něj se web nesestaví.
3. **Spuštění:** Actions → Krizový hlídač → *Run workflow*. Pak už běží samo každých 10 minut. Po přidání kanálu upozornění zaškrtni při ručním spuštění *Poslat zkušební upozornění* a výsledek uvidíš v souhrnu běhu.
4. Otevři `https://<uživatel>.github.io/krizovy-hlidac/?k=<DASH_TOKEN>` v Safari → Sdílet → **Přidat na plochu**. Pokud aplikace na ploše chce kód, vlož ho jednou a zapamatuje si ho.

**Osobní konfigurace** (místa, trasa) nemusí být ve veřejném `config.json`, ten je jen ukázka. Hlídač ji hledá v tomto pořadí:
- secret `CONFIG_JSON` – celý obsah konfigurace,
- `config.enc.json` v repu – konfigurace zašifrovaná kódem `DASH_TOKEN`, bez něj nečitelná:
  ```bash
  DASH_TOKEN=… python3 hlidac.py --encrypt-config muj-config.json   # vytvoří config.enc.json
  DASH_TOKEN=… python3 hlidac.py --decrypt-config > muj-config.json # pro další úpravy
  ```
- `config.json`.

**Upozornění:** secrets `PUSHOVER_TOKEN` + `PUSHOVER_USER`, nebo `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`, nebo `NTFY_TOPIC`. Můžou být i v šifrované konfiguraci v sekci `"env": {"PUSHOVER_TOKEN": "…", "PUSHOVER_USER": "…"}`. Volitelně `HEALTHCHECK_URL` a `DASHBOARD_URL` (jinak se použije adresa na GitHub Pages i s kódem).

### B) Vlastní server

```bash
cp .env.example .env && nano .env        # token, kanály, DASHBOARD_URL
docker compose up -d --build             # nebo systemd: hlidac.service
docker compose exec hlidac python hlidac.py --test-notify
```

Bez Dockeru stačí Python 3.9+ bez dalších knihoven: `python3 hlidac.py`. Dashboard běží na portu 8080. Kvůli offline režimu na iPhonu ho dej za HTTPS, třeba přes Caddy:

```
hlidac.example.cz {
    reverse_proxy localhost:8080
}
```

---

## Upozornění na mobil

**Pushover (doporučeno pro iPhone):** umí kritická upozornění, která projdou přes ztlumení i režim Nerušit. Nainstaluj aplikaci (po 30denní zkušební době se platí jednorázově), na pushover.net zkopíruj *Your User Key* → `PUSHOVER_USER` a vytvoř aplikaci → `PUSHOVER_TOKEN`. V aplikaci zapni v Nastavení **Critical Alerts**.

**Telegram (zdarma):** u @BotFather vytvoř bota → `TELEGRAM_TOKEN`. Napiš botovi cokoli a otevři `https://api.telegram.org/bot<TOKEN>/getUpdates`. Číslo `chat.id` → `TELEGRAM_CHAT_ID`.

**ntfy (zdarma, bez účtu):** vymysli dlouhý náhodný název tématu (aspoň 20 znaků). Kdo název zná, čte tvoje zprávy, takže ho ber jako heslo. Téma odebírej v aplikaci ntfy (+ → Subscribe to topic) a stejný název dej do `NTFY_TOPIC`. Odkaz ve zprávě kód k webu neobsahuje, prohlížeč si ho pamatuje z prvního otevření. Na iOS ntfy nepodporuje naléhavé priority, proto v Nastavení → Soustředění → Nerušit → Aplikace přidej ntfy mezi povolené.

Hlídač pošle zprávu i když nějaký zdroj nefunguje déle než 6 hodin a každou neděli v 18:00 krátké „běžím“. Na healthchecks.io (zdarma) si můžeš nastavit hlídání, jestli hlídač běží.

## Útočiště, trasy a offline mapy

- **`utocista`**: seznam útočišť podle preference (`code`, `name`, `trasa`, `pozn`). Doporučí se nejklidnější, při shodě to první v seznamu. Ke každému kódu patří zdroje: Meteoalarm, GDACS, Auswärtiges Amt, US State Dept a pravidla zpráv.
- **`plan.trasy`**: trasy s body (`nazev`, `lat`, `lon`) a německými dálnicemi (`dalnice`), které se hlídají přes Autobahn API (uzavírky a kolony nad 30 minut). České silnice API nemají.
- V dashboardu je u každé trasy **Uložit do offline map (GPX)**. Na iPhonu se otevře sdílení, kde zvolíš OsmAnd nebo Organic Maps. Druhé tlačítko otevře trasu v Google Mapách.
- **Palivo v Německu** (volitelné): zaregistruj se zdarma na creativecommons.tankerkoenig.de a klíč dej do secretu `TANKERKOENIG_KEY`. Hlídač pak u bodů z `fuel.points` sleduje, kolik pump je otevřených a jestli nafta prudce nezdražila. API dovoluje jeden dotaz za minutu, takže se při každém běhu ptá na jeden bod.
- **`zony`** u trasy: poznámky k nízkoemisním zónám a místům, kde je potřeba něco vyřídit předem (Crit'Air pro Francii, registrace ZBE v Barceloně). Body tras vedou mimo centra měst se zákazy – dashboard je ukáže pod nadpisem *Pozor na trase*.

## Připravenost

- **`pripravenost.polozky`**: doklady a věci k vyřízení. Položka s `plati_do` (datum `2028-05-31`, `null` = chybí) se hlídá: připomínka přijde 60, 30 a 7 dní předem a v den konce (`upozornit_dni`). Položka s `hotovo: false` je úkol (Crit'Air, ZBE), `volitelne: true` = až bude potřeba. Nevyřízené věci se připomenou v nedělním „běžím“ a od stupně 🟠 i v upozornění („Než vyrazíš: …“).
- **Seznamy na 72 hodin** (nouzové zásoby, evakuační zavazadlo, dítě, auto, telefon) jsou v dashboardu k odškrtávání, zaškrtnutí si pamatuje telefon. Vlastní seznamy jdou nastavit v `seznamy`. Podklady: příručka 72 hodin Ministerstva vnitra.

## Úpravy konfigurace

- **`regions`, `plan`**: názvy oblastí, cíl, trasa (zastávky odděluj šipkou →), záložní trasa, místo srazu.
- **`nina.areas`**: německé okresy podle kódu ARS (5 číslic okresu + 7 nul, seznam „Regionalschlüssel“ od Destatis).
- **`meteoalarm.feeds`**: `area_regex` vybere kraj (malými písmeny bez diakritiky, např. `karlovarsk`, `praha`).
- **`gdacs`, `auswaertiges_amt`, `us_state`**: sledované země a jejich oblast.
- **`bfs_radiation`**: radiace z německé sítě BfS. `zones` jsou výřezy mapy (např. hranice u domova, Šumava ve směru od Temelína) a k nim prahy: ≥ `min_stations` sond nad `warn_usvh` → 🟠, nad `alarm_usvh` → 🔴. Hlídač si u každé sondy pamatuje její normál a ≥ `rise_min_stations` sond aspoň `rise_factor`× nad normálem hlásí jako 🟡 (často jde o silný déšť). Běžné pozadí je 0,05–0,2 µSv/h. U radiace upozornění radí nejdřív se ukrýt, ne hned odjíždět. Česká síť SÚJB ani rakouská nemají otevřené rozhraní, dashboard na ně odkazuje.
- **`chmi_hydro`**: vodočty ČHMÚ (ID stanice z `opendata.chmi.cz/hydrology/now/metadata/meta1.json`). Hranice SPA se načtou samy. `levels` převádí SPA na stupeň hlídače (výchozí 2. SPA → 🟡, 3. SPA → 🟠, extrémní povodeň → 🔴), hlídá se i předpověď na 48 h a rychlé stoupání hladiny.
- **`news.rules`**: pravidla pro titulky. Regulární výrazy piš **malými písmeny bez diakritiky**. `single` = stupeň při 1 zdroji, `confirmed` = při ≥ 2 nezávislých zdrojích, `feeds` = jen pro vybrané kanály.
- **`gas`**: zásobníky plynu (GIE AGSI+). Klíč zdarma po registraci na agsi.gie.eu → secret `AGSI_KEY` (nebo `"env": {"AGSI_KEY": "…"}` v šifrované konfiguraci). Pod `sledovat_pod_pct` → 🟡, pod `pripravit_pod_pct` → 🟠; totéž pro počet dní, na které by zásobníky vystačily při dnešním odběru.
- **`internet`**: výpadky internetu celých zemí (IODA). Kritický pokles ve dvou měřeních → 🟠, v jednom → 🟡, samotné teleskopy se ignorují.
- **`gdelt.dotazy`**: témata, u kterých se hlídá prudký nárůst zpráv ve světě (GDELT, 65 jazyků). Dotaz v angličtině, `pomer` = kolikrát víc než běžný den. Každý běh se ptá na jedno téma. Ve výchozím stavu vypnuto: z GitHub Actions GDELT dotazy odmítá (HTTP 429), na vlastním serveru zapni `enabled: true`.
- **`nukib`, `psp`, `easa`**: úřední signály – varování NÚKIB před útoky, nové sněmovní tisky o stavu ohrožení státu nebo mobilizaci (první běh si zapamatuje výchozí stav a staré tisky nehlásí) a bulletiny EASA o konfliktních zónách v Evropě.
- **`zemetreseni`**: otřesy v okolí domova (EMSC). Výchozí výřez je Chebsko a Vogtland, nejaktivnější seismická oblast Česka.
- **`notify.repeat_level3_minutes`**: jak často opakovat upozornění, dokud trvá 🔴 (0 = vypnuto).
- **`notify.min_level`**: od jakého stupně posílat upozornění (výchozí 1).

## Zdroje

| Zdroj | Co hlídá |
|---|---|
| NINA / BBK (warnung.bund.de) | výstrahy civilní ochrany ve vybraných okresech, celostátní MoWaS |
| Meteoalarm | výstrahy ČHMÚ pro vybraný kraj, AEMET pro Španělsko |
| GDACS | povodně, zemětřesení a další katastrofy |
| NOAA SWPC | geomagnetické bouře, které ohrožují rozvodné sítě |
| Auswärtiges Amt, US State Dept | cestovní varování |
| BfS ODL | radiace: zóny u českých hranic a na Šumavě, celé Německo, nárůst proti normálu sondy |
| ČHMÚ (otevřená data) | vodočty, stupně povodňové aktivity a předpověď |
| Autobahn GmbH | uzavírky a kolony na německých dálnicích tras |
| Tankerkönig (CC BY 4.0) | otevřené pumpy a ceny nafty na trase (volitelné) |
| GIE AGSI+ | plyn v zásobnících v Německu, Česku a EU (volitelné, klíč zdarma) |
| IODA (Georgia Tech) | výpadky internetu celých zemí – nezávislý signál blackoutu nebo kyberútoku |
| GDELT | prudký nárůst světových zpráv na sledovaná témata |
| NÚKIB | úřední varování před kybernetickými útoky |
| Poslanecká sněmovna (otevřená data) | nové sněmovní tisky: stav ohrožení státu, válečný stav, mobilizace |
| EASA | bulletiny o konfliktních zónách pro civilní letectví |
| EMSC | zemětřesení v okolí domova |
| Natural Earth (world-atlas) | podklad mapy v dashboardu (`web/mapa.json`, volné dílo) |
| ČT24, iROZHLAS, Novinky, Tagesschau, MDR, El País, RTP, RTÉ, BBC, MAAE, SÚJB | titulky podle pravidel, potvrzení = 2 nezávislé zdroje |

Nefunkční zdroj ukáže dashboard v části Zdroje. Když jich nefunguje většina, místo „Klid“ ukáže „Bez dat“.

## ESP32 semafor (volitelné)

`esp32-semafor/semafor.ino` je kontrolka doma: LED WS2812 na GPIO5, bzučák na GPIO18. Svítí barvou stupně, při 🔴 bliká a pípá a tlačítko BOOT ji ztiší. Fialové blikání znamená, že hlídač neodpovídá. Funguje s vlastním serverem (čte `status.json`), šifrovaný stav z GitHub Pages neumí.

## Limity

- **Náhlý blackout přijde bez varování.** Hlídač dá čas u války, plynové krize, povodní a geomagnetických bouří. Na náhlý výpadek proudu jsou zásoby doma.
- Pravidla pro titulky zachytí i úvahy typu „Co kdyby…“. Proto 1 zdroj = nižší stupeň s poznámkou „ověř“.
- Hlídač nenahrazuje oficiální varování: aplikaci NINA, výstražné zprávy v telefonu (Cell Broadcast, v Česku se spouští) a sirény. V telefonu měj nouzová upozornění zapnutá.

## Vývoj

```bash
pip install cryptography                    # jen pro šifrovaný web (--site)
python3 -m unittest discover -s tests -v    # testy: zdroje, pravidla, upozornění, server, šifrování
python3 hlidac.py --demo                    # ukázková data v dashboardu na :8080, bez sítě
```
