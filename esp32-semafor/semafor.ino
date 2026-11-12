// Krizový semafor – ESP32 + LED pásek/kroužek WS2812, volitelně bzučák.
// Zobrazuje celkový stupeň z hlídače: zelená / žlutá / oranžová / červená (bliká + pípá).
// Fialové blikání = hlídač neodpovídá víc než 30 min nebo většina zdrojů nefunguje.
// Tlačítko BOOT ztiší pípání, dokud se stupeň nezmění.
//
// Knihovny (Správce knihoven v Arduino IDE): „Adafruit NeoPixel“, „ArduinoJson“ (verze 7).
// Deska: ESP32 Dev Module (nebo ESP32-S3 – jen uprav piny).

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>

// ── nastav ────────────────────────────────────────────────
const char* WIFI_SSID  = "TvojeWiFi";
const char* WIFI_PASS  = "heslo";
const char* STATUS_URL = "https://hlidac.example.cz/status.json?k=TVUJ_TOKEN";

const int LED_PIN = 5;        // DIN pásku
const int LED_COUNT = 8;
const int BUZZER_PIN = 18;    // aktivní/pasivní bzučák, druhý vývod na GND
const int BUTTON_PIN = 0;     // tlačítko BOOT
const uint8_t BRIGHTNESS = 60;
// ──────────────────────────────────────────────────────────

const uint32_t POLL_MS = 60UL * 1000;
const uint32_t OFFLINE_MS = 30UL * 60 * 1000;

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
int level = -1;
bool blind = false;
bool muted = false;
uint32_t lastOk = 0;
uint32_t lastPoll = 0;
bool everOk = false;

uint32_t colorFor(int l) {
  switch (l) {
    case 0: return strip.Color(0, 120, 40);
    case 1: return strip.Color(200, 150, 0);
    case 2: return strip.Color(230, 70, 0);
    case 3: return strip.Color(220, 0, 0);
    default: return strip.Color(90, 0, 140);
  }
}

void fill(uint32_t c) {
  for (int i = 0; i < LED_COUNT; i++) strip.setPixelColor(i, c);
  strip.show();
}

bool poll() {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  String url = STATUS_URL;
  bool begun;
  if (url.startsWith("https")) {
    secure.setInsecure();          // jen čteme veřejně neškodný stav; certifikát neověřujeme
    begun = http.begin(secure, url);
  } else {
    begun = http.begin(plain, url);
  }
  if (!begun) return false;
  http.setTimeout(15000);
  int code = http.GET();
  if (code != 200) { http.end(); return false; }

  JsonDocument filter;
  filter["overall"]["level"] = true;
  filter["blind"] = true;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) return false;

  int newLevel = doc["overall"]["level"] | 0;
  blind = doc["blind"] | false;
  if (newLevel != level) muted = false;
  level = newLevel;
  return true;
}

void setup() {
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  strip.begin();
  strip.setBrightness(BRIGHTNESS);
  fill(colorFor(-1));
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  uint32_t now = millis();

  if (digitalRead(BUTTON_PIN) == LOW) { muted = true; noTone(BUZZER_PIN); }

  uint32_t interval = everOk ? POLL_MS : 5000;   // dokud se nepřipojí, zkouší každých 5 s
  if (lastPoll == 0 || now - lastPoll >= interval) {
    lastPoll = now ? now : 1;
    if (poll()) { lastOk = now; everOk = true; }
  }

  bool offline = !everOk || (now - lastOk > OFFLINE_MS);
  bool blinkOn = (now / 500) % 2;

  if (offline || blind) {
    noTone(BUZZER_PIN);
    fill(blinkOn ? colorFor(-1) : 0);
  } else if (level >= 3) {
    fill(blinkOn ? colorFor(3) : 0);
    // první 3 sekundy každé minuty pípá, dokud ho neztišíš tlačítkem
    if (!muted && (now / 1000) % 60 < 3) tone(BUZZER_PIN, blinkOn ? 2000 : 1500);
    else noTone(BUZZER_PIN);
  } else {
    noTone(BUZZER_PIN);
    fill(colorFor(level));
  }
  delay(50);
}
