# Krizový hlídač

Osobní systém včasného varování před válkou, rozsáhlým blackoutem a katastrofami. Každých 10 minut projde oficiální zdroje i zprávy a ohodnotí situaci stupněm 0–3 pro domov, práci, Evropu a zvolené útočiště. Když se něco zhorší, pošle upozornění na mobil. Webový dashboard ukazuje stav, co dělat teď a plán odjezdu.

| Stupeň | Význam | Typický spouštěč |
|---|---|---|
| 🟢 Klid | nic neobvyklého | — |
| 🟡 Sleduj | zvýšené napětí | výstraha ČHMÚ, geomagnetická bouře G4, jednotlivá zpráva o výpadku nebo útoku |
| 🟠 Připrav se | hrozba v řádu dnů | výstraha NINA „Extreme“, zvýšená radiace u hranic, zpráva o stavu ohrožení státu z 1 zdroje, předpověď bouře G5 |
| 🔴 Odjeď | bezprostřední hrozba | letecký poplach v Německu, potvrzený návrh stavu ohrožení státu, cestovní varování pro ČR, čl. 5 NATO |

Celkový stav se počítá z domova, práce a Evropy. Útočiště se ukazuje zvlášť: problém tam znamená „jeď jinam“, ne „uteč z domova“.

**Proč spěchat u stavu ohrožení státu:** za stavu ohrožení státu nebo válečného stavu potřebuje člověk s brannou povinností k cestě do ciziny souhlas krajského vojenského velitelství (branný zákon č. 585/2004 Sb.) a na souhlas není nárok. Proto už potvrzená zpráva o *návrhu* vlády znamená 🔴.

---

## Dva způsoby provozu

**A) GitHub, zdarma a bez serveru.** GitHub Actions spouští hlídač každých 10 minut a web běží na GitHub Pages. Osobní konfigurace je v šifrovaném secretu a stav na webu je zašifrovaný kódem, takže veřejně čitelný není ani ve veřejném repu. Nevýhoda: GitHub plánované běhy občas o pár minut zpozdí.

**B) Vlastní server (VPS, Raspberry Pi, hosting).** Hlídač běží nepřetržitě a dashboard obsluhuje sám. Server mimo domov přežije výpadek proudu u tebe.

### A) GitHub

1. **Pages:** Settings → Pages → Source: *GitHub Actions*.
2. **Secret** (Settings → Secrets and variables → Actions → New repository secret): `DASH_TOKEN` – kód k webu, dlouhý náhodný řetězec (`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`). Bez něj se web nesestaví.
3. **Spuštění:** Actions → Krizový hlídač → *Run workflow*. Pak už běží samo každých 10 minut.
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

**ntfy (Android nebo záloha):** do `NTFY_TOPIC` dej dlouhý tajný název a stejné téma odebírej v aplikaci ntfy. Na iOS nepodporuje naléhavé priority.

Hlídač pošle zprávu i když nějaký zdroj nefunguje déle než 6 hodin a každou neděli v 18:00 krátké „běžím“. Na healthchecks.io (zdarma) si můžeš nastavit hlídání, jestli hlídač běží.

## Úpravy konfigurace

- **`regions`, `plan`**: názvy oblastí, cíl, trasa (zastávky odděluj šipkou →), záložní trasa, místo srazu.
- **`nina.areas`**: německé okresy podle kódu ARS (5 číslic okresu + 7 nul, seznam „Regionalschlüssel“ od Destatis).
- **`meteoalarm.feeds`**: `area_regex` vybere kraj (malými písmeny bez diakritiky, např. `karlovarsk`, `praha`).
- **`gdacs`, `auswaertiges_amt`, `us_state`**: sledované země a jejich oblast.
- **`bfs_radiation`**: výřez mapy a prahy radiace. Běžné pozadí je 0,05–0,2 µSv/h, déšť ho krátce zvedne asi na 0,3.
- **`news.rules`**: pravidla pro titulky. Regulární výrazy piš **malými písmeny bez diakritiky**. `single` = stupeň při 1 zdroji, `confirmed` = při ≥ 2 nezávislých zdrojích.
- **`notify.min_level`**: od jakého stupně posílat upozornění (výchozí 1).

## Zdroje

| Zdroj | Co hlídá |
|---|---|
| NINA / BBK (warnung.bund.de) | výstrahy civilní ochrany ve vybraných okresech, celostátní MoWaS |
| Meteoalarm | výstrahy ČHMÚ pro vybraný kraj, AEMET pro Španělsko |
| GDACS | povodně, zemětřesení a další katastrofy |
| NOAA SWPC | geomagnetické bouře, které ohrožují rozvodné sítě |
| Auswärtiges Amt, US State Dept | cestovní varování |
| BfS ODL | měření radiace v Německu, výřez u českých hranic |
| ČT24, iROZHLAS, Novinky, Tagesschau, MDR, El País, BBC | titulky podle pravidel, potvrzení = 2 nezávislé zdroje |

Nefunkční zdroj ukáže dashboard v části Zdroje. Když jich nefunguje většina, místo „Klid“ ukáže „Bez dat“.

## ESP32 semafor (volitelné)

`esp32-semafor/semafor.ino` je kontrolka doma: LED WS2812 na GPIO5, bzučák na GPIO18. Svítí barvou stupně, při 🔴 bliká a pípá a tlačítko BOOT ji ztiší. Fialové blikání znamená, že hlídač neodpovídá. Funguje s vlastním serverem (čte `status.json`), šifrovaný stav z GitHub Pages neumí.

## Limity

- **Náhlý blackout přijde bez varování.** Hlídač dá čas u války, plynové krize, povodní a geomagnetických bouří. Na náhlý výpadek proudu jsou zásoby doma.
- Pravidla pro titulky zachytí i úvahy typu „Co kdyby…“. Proto 1 zdroj = nižší stupeň s poznámkou „ověř“.
- Hlídač nenahrazuje oficiální varování: aplikaci NINA, výstražné zprávy v telefonu (Cell Broadcast) a sirény.

## Vývoj

```bash
pip install cryptography                    # jen pro šifrovaný web (--site)
python3 -m unittest discover -s tests -v    # testy: zdroje, pravidla, upozornění, server, šifrování
python3 hlidac.py --demo                    # ukázková data v dashboardu na :8080, bez sítě
```
