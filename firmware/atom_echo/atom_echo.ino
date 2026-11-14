#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Adafruit_NeoPixel.h>
#include <driver/i2s.h>
#include <math.h>

#define WIFI_SSID   "DOMACI_WIFI"
#define WIFI_PASS   "heslo"
#define HELPBUTTON_HOST   ""
#define HELPBUTTON_PORT   8765
#define HELPBUTTON_API_KEY ""

#define PIN_BUTTON   39
#define PIN_LED      27
#define PIN_I2S_BCK  19
#define PIN_I2S_LRCK 33
#define PIN_SPK_DATA 22
#define PIN_MIC_DATA 23

#define MIC_RATE          16000
#define MAX_SECONDS       10
#define I2S_PORT          I2S_NUM_0
#define PCM_CHUNK_SAMPLES 1024
#define RELEASE_DEBOUNCE_MS 35

Adafruit_NeoPixel led(1, PIN_LED, NEO_GRB + NEO_KHZ800);
String host = HELPBUTTON_HOST;
uint16_t port = HELPBUTTON_PORT;
int16_t buf[PCM_CHUNK_SAMPLES];
uint8_t txFrame[16 + PCM_CHUNK_SAMPLES * 2 + 2];

void setLed(uint8_t r, uint8_t g, uint8_t b) { led.setPixelColor(0, led.Color(r, g, b)); led.show(); }
void ledByName(const String &n) {
  if (n == "green") setLed(0, 120, 30);
  else if (n == "red") setLed(150, 0, 0);
  else if (n == "blue" || n == "blink") setLed(0, 40, 160);
  else setLed(0, 0, 0);
}

void i2sMic() {
  i2s_driver_uninstall(I2S_PORT);
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
  cfg.sample_rate = MIC_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ALL_RIGHT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 6;
  cfg.dma_buf_len = 256;
  i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_PIN_NO_CHANGE;
  pins.ws_io_num = PIN_I2S_LRCK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DATA;
  i2s_set_pin(I2S_PORT, &pins);
  i2s_set_clk(I2S_PORT, MIC_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
}

void i2sSpeaker(uint32_t rate) {
  i2s_driver_uninstall(I2S_PORT);
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = rate;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ALL_RIGHT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;
  cfg.tx_desc_auto_clear = true;
  i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_BCK;
  pins.ws_io_num = PIN_I2S_LRCK;
  pins.data_out_num = PIN_SPK_DATA;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  i2s_set_pin(I2S_PORT, &pins);
  i2s_set_clk(I2S_PORT, rate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
}

void tone(float freq, int ms) {
  i2sSpeaker(16000);
  const int total = 16000 * ms / 1000;
  size_t written;
  for (int i = 0; i < total; i += 256) {
    int n = min(256, total - i);
    for (int k = 0; k < n; k++) buf[k] = (int16_t)(6000 * sinf(2 * M_PI * freq * (i + k) / 16000.0f));
    i2s_write(I2S_PORT, buf, n * 2, &written, portMAX_DELAY);
  }
  i2s_zero_dma_buffer(I2S_PORT);
}
void beep()     { tone(1200, 80); }
void beepTwice(){ tone(1200, 100); delay(80); tone(1600, 120); }
void lowTone()  { tone(330, 400); }

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  setLed(40, 20, 0);
  while (WiFi.status() != WL_CONNECTED) { delay(300); }
  setLed(0, 0, 0);
  if (host.length() == 0 && MDNS.begin("helpbutton")) {
    int n = MDNS.queryService("helpbutton", "tcp");
    if (n > 0) { host = MDNS.IP(0).toString(); port = MDNS.port(0); }
  }
}

bool notifyPress() {
  HTTPClient http;
  http.setConnectTimeout(250);
  http.setTimeout(350);
  http.begin("http://" + host + ":" + String(port) + "/api/press");
  http.addHeader("Content-Type", "application/json");
  if (strlen(HELPBUTTON_API_KEY)) http.addHeader("Authorization", String("Bearer ") + HELPBUTTON_API_KEY);
  int code = http.POST("{\"source\":\"button\"}");
  String body = (code == 200) ? http.getString() : "";
  http.end();
  if (code == 401 || code == 403) {
    setLed(150, 0, 0);
    lowTone();
    setLed(0, 0, 0);
    return true;
  }
  if (code == 200 && body.indexOf("Zastavila") >= 0) {
    beepTwice();
    setLed(0, 0, 0);
    return true;
  }
  return false;
}

String readLine(WiFiClient &c) {
  String s = c.readStringUntil('\n');
  s.trim();
  return s;
}

void talk() {
  WiFiClient c;
  if (!c.connect(host.c_str(), port)) { setLed(150, 0, 0); lowTone(); return; }
  c.setNoDelay(true);
  c.printf("POST /api/button/audio?rate=%d HTTP/1.1\r\nHost: %s\r\n", MIC_RATE, host.c_str());
  if (strlen(HELPBUTTON_API_KEY)) c.printf("Authorization: Bearer %s\r\n", HELPBUTTON_API_KEY);
  c.print("Content-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n");
  i2sMic();
  size_t got = 0;
  i2s_read(I2S_PORT, buf, 512, &got, portMAX_DELAY);
  uint32_t start = millis();
  uint32_t releaseAt = 0;
  while (millis() - start < MAX_SECONDS * 1000) {
    if (digitalRead(PIN_BUTTON) == HIGH) {
      if (releaseAt == 0) releaseAt = millis();
      else if (millis() - releaseAt >= RELEASE_DEBOUNCE_MS) break;
    } else {
      releaseAt = 0;
    }
    i2s_read(I2S_PORT, buf, sizeof(buf), &got, portMAX_DELAY);
    if (got == 0) continue;
    int hdrLen = snprintf((char *)txFrame, 16, "%X\r\n", (unsigned)got);
    memcpy(txFrame + hdrLen, buf, got);
    txFrame[hdrLen + got] = '\r';
    txFrame[hdrLen + got + 1] = '\n';
    c.write(txFrame, hdrLen + got + 2);
  }
  c.print("0\r\n\r\n");
  c.flush();
  i2s_driver_uninstall(I2S_PORT);
  setLed(0, 20, 80);

  uint32_t t0 = millis();
  while (!c.available() && c.connected() && millis() - t0 < 60000) delay(10);
  String status = readLine(c);
  String ledName = "off", toneName = "none";
  int length = -1;
  for (String h = readLine(c); h.length() > 0; h = readLine(c)) {
    String l = h; l.toLowerCase();
    if (l.startsWith("x-helpbutton-led:")) ledName = h.substring(17), ledName.trim();
    else if (l.startsWith("x-helpbutton-tone:")) toneName = h.substring(18), toneName.trim();
    else if (l.startsWith("content-length:")) length = h.substring(15).toInt();
  }
  if (status.indexOf(" 401") > 0 || status.indexOf(" 403") > 0) {
    ledByName("red");
    lowTone();
    c.stop();
    setLed(0, 0, 0);
    return;
  }
  ledByName(ledName);
  if (toneName == "double") beepTwice();
  else if (toneName == "low") lowTone();

  if (status.indexOf(" 200") > 0 && length > 44) {
    uint8_t hdr[44];
    c.readBytes(hdr, 44);
    uint32_t rate = hdr[24] | (hdr[25] << 8) | (hdr[26] << 16) | (hdr[27] << 24);
    i2sSpeaker((rate >= 8000 && rate <= 48000) ? rate : 22050);
    int left = length - 44;
    size_t written;
    while (left > 0 && (c.connected() || c.available())) {
      int n = c.read((uint8_t *)buf, min((int)sizeof(buf), left));
      if (n <= 0) { delay(2); continue; }
      i2s_write(I2S_PORT, buf, n, &written, portMAX_DELAY);
      left -= n;
    }
    i2s_zero_dma_buffer(I2S_PORT);
  }
  c.stop();
  if (ledName != "blue") { delay(1500); setLed(0, 0, 0); }
}

void setup() {
  pinMode(PIN_BUTTON, INPUT);
  led.begin();
  led.setBrightness(60);
  connectWifi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (digitalRead(PIN_BUTTON) == LOW) {
    setLed(0, 40, 160);
    beep();
    if (!notifyPress()) talk();
    while (digitalRead(PIN_BUTTON) == LOW) delay(10);
  }
  delay(10);
}
