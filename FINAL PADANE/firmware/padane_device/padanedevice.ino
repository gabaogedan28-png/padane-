#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>       // Finds the PC by name (e.g. MY-PC.local)
#include <Preferences.h>   // Remembers the last working server IP in flash
#include <esp_sleep.h>     // Required for light sleep features
#include <driver/gpio.h>   // Required for GPIO wake-up configurations

// ============================================================================
// WI-FI CREDENTIALS
// This MUST be the same Wi-Fi network your Flask PC is connected to.
// ============================================================================
const char* ssid = "HUAWEI-W3ck";
const char* password = "4WdmZ2hm";

// ============================================================================
// SERVER DISCOVERY
// The PC's IP changes every day on a phone hotspot, so the ESP32 no longer
// relies on one hardcoded address. On boot (and whenever a request fails) it
// tries, in order:
//   1. the last IP that worked (saved in flash)
//   2. the PC's name via mDNS  -> SERVER_HOSTNAME.local
//   3. the Wi-Fi gateway       -> covers a Windows "Mobile hotspot" (192.168.137.1)
//   4. FALLBACK_SERVER_IP
// ============================================================================
const char* SERVER_HOSTNAME    = "LAPTOP-QGMIAPA9";  // Windows: Settings > System > About > Device name (no ".local")
const char* FALLBACK_SERVER_IP = "10.63.68.53";   // last resort
const uint16_t SERVER_PORT     = 5000;

Preferences prefs;
String serverIP = "";   // filled in by discoverServer()

const String deviceSerial = "PAD-451107";

// --- Parsed GNSS Fix ---
double currentLat = 0.0;
double currentLng = 0.0;
bool hasFix = false;
bool hasEverFixed = false; // true once we've locked GPS at least once since boot

// --- Pins & Hardware ---
const int buttonPin = 4;
#define MODEM_RX 17
#define MODEM_TX 18
#define MODEM_BAUDRATE 115200

// --- SMS Configuration ---
const String PHONE_NUMBER = "+639938719186";

// --- Buzzer Configuration ---
const String SIMTONE_CMD = "AT+SIMTONE=1,2000,1000,0,1000";

// --- Modem Object ---
HardwareSerial modemSerial(1);

// --- Global Variables ---
String currentCoordinates = "Waiting for initial GPS lock...";
const unsigned long gnssInterval = 10000; // Sleep interval (10 seconds)

// Function Declarations
void ensureWiFiConnected(bool force = false);
String apiUrl(const char* path);
bool pingServer(const String &ip);
bool useServer(const String &ip);
bool discoverServer();
void serialConfigWindow();
void checkBackendReachable();
void updateGNSSLocation();
bool parseGNSSToDecimal(const String &data, double &outLat, double &outLng);
String fetchAddress(double lat, double lng);
String buildSOSMessage();
String jsonEscape(const String &s);
String buildPayload();
void sendSMS(String number, String message);
void sendToFlaskBackend(const char* path);
bool sendATCommand(String command, String expectedResponse, unsigned long timeout);

void setup() {
  Serial.begin(115200);
  delay(1000);

  // FIX FOR BOOT LOOP: Initialize Wi-Fi state immediately to prevent memory panics
  WiFi.mode(WIFI_STA);

  Serial.println("\n--- ESP32 Starting Up (Direct IoT Node Mode) ---");

  prefs.begin("padane", false);
  serialConfigWindow();   // optional: type "ip x.x.x.x" to set the server by hand

  // Configure Button Pin
  pinMode(buttonPin, INPUT_PULLUP);

  // Configure GPIO Wakeup for Light Sleep
  esp_sleep_enable_gpio_wakeup();
  gpio_wakeup_enable((gpio_num_t)buttonPin, GPIO_INTR_LOW_LEVEL);

  // Initial Wi-Fi Connection (also discovers the Flask server)
  ensureWiFiConnected();

  // Confirm the Flask backend is actually reachable before doing anything else.
  checkBackendReachable();

  // Initialize Modem
  Serial.println("Initializing A7670E 4G modem...");
  modemSerial.begin(MODEM_BAUDRATE, SERIAL_8N1, MODEM_RX, MODEM_TX);
  delay(3000);

  if (sendATCommand("AT", "OK", 2000)) {
    Serial.println("Modem responding. Powering on GNSS & configuring SMS...");
    sendATCommand("AT+CGNSSPWR=1", "OK", 2000);
    sendATCommand("AT+CMGF=1", "OK", 2000);
  } else {
    Serial.println("Modem failed to respond.");
  }
}

void loop() {
  // Check why the ESP32 woke up
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();

  // Treat as button press if GPIO woke us up or if the pin is currently held LOW
  bool isButtonTrigger = (wakeup_reason == ESP_SLEEP_WAKEUP_GPIO) || (digitalRead(buttonPin) == LOW);

  if (isButtonTrigger) {
    Serial.println("\n[WAKEUP] High Priority: Panic Button Pressed!");

    // 1. Force a GNSS read.
    updateGNSSLocation();
    int gpsRetries = 0;
    while (!hasFix && !hasEverFixed && gpsRetries < 6) {
      Serial.println("[SOS] No GPS fix yet, retrying (" + String(gpsRetries + 1) + "/6)...");
      delay(1000);
      updateGNSSLocation();
      gpsRetries++;
    }
    if (hasFix) hasEverFixed = true;

    // 2. Action A: Send cellular text alert
    String messagePayload = buildSOSMessage();
    sendSMS(PHONE_NUMBER, messagePayload);

    // 3. Action B: Post directly to the Flask Backend's TRIGGER route
    ensureWiFiConnected();
    sendToFlaskBackend("/api/trigger-hardware");

  } else {
    // Regular 10-second timer wake up
    Serial.println("\n[WAKEUP] Routine Periodic Telemetry Check...");
    updateGNSSLocation();
    if (hasFix) hasEverFixed = true;

    // Heartbeat to the backend's TELEMETRY route
    ensureWiFiConnected();
    sendToFlaskBackend("/api/iot/telemetry");
  }

  // --- PREPARE FOR LIGHT SLEEP ---
  Serial.println("Entering Light Sleep mode... GNSS remains active.");
  Serial.flush(); // Ensure serial logs are fully sent before pausing the CPU clock

  // Light sleep kills the Wi-Fi link, which left a stale "connected" state and
  // lost every other heartbeat. Shut Wi-Fi down cleanly; it reconnects on wake.
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  // Set timer wakeup interval
  esp_sleep_enable_timer_wakeup(gnssInterval * 1000);

  // Execute light sleep
  esp_light_sleep_start();
}

// ============================================================================
// SERVER DISCOVERY HELPERS
// ============================================================================

String apiUrl(const char* path) {
  return "http://" + serverIP + ":" + String(SERVER_PORT) + path;
}

// Returns true if a Flask server answers /api/ping at this IP.
bool pingServer(const String &ip) {
  if (ip.length() == 0 || WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.begin("http://" + ip + ":" + String(SERVER_PORT) + "/api/ping");
  http.setConnectTimeout(1500);
  http.setTimeout(1500);
  int code = http.GET();
  http.end();
  return code == 200;
}

// Adopts an IP as the server and saves it so the next boot starts there.
bool useServer(const String &ip) {
  serverIP = ip;
  if (prefs.getString("serverIP", "") != ip) {
    prefs.putString("serverIP", ip);
  }
  Serial.println("[Discovery] Using Flask server at " + ip);
  return true;
}

bool discoverServer() {
  if (WiFi.status() != WL_CONNECTED) return false;
  Serial.println("[Discovery] Looking for the Flask server...");

  // 1. Last IP that worked
  String saved = prefs.getString("serverIP", "");
  if (pingServer(saved)) return useServer(saved);

  // 2. mDNS: ask the network for SERVER_HOSTNAME.local
  static bool mdnsStarted = false;
  if (!mdnsStarted) mdnsStarted = MDNS.begin("padane-device");
  if (mdnsStarted) {
    IPAddress found = MDNS.queryHost(SERVER_HOSTNAME, 2000);
    String foundStr = found.toString();
    if (foundStr != "0.0.0.0" && pingServer(foundStr)) return useServer(foundStr);
  }

  // 3. Wi-Fi gateway (the PC itself when it is hosting the hotspot)
  String gateway = WiFi.gatewayIP().toString();
  if (pingServer(gateway)) return useServer(gateway);

  // 4. Hardcoded fallback
  if (pingServer(FALLBACK_SERVER_IP)) return useServer(FALLBACK_SERVER_IP);

  Serial.println("[Discovery] Flask server not found.");
  serverIP = "";
  return false;
}

// For a few seconds at boot, lets you set the server IP from the Serial Monitor.
// Type:  ip 192.168.1.20   (or)   clear
void serialConfigWindow() {
  Serial.println("Type 'ip x.x.x.x' within 4 seconds to set the server IP manually.");
  unsigned long end = millis() + 4000;
  while (millis() < end) {
    if (Serial.available()) {
      String line = Serial.readStringUntil('\n');
      line.trim();
      if (line.startsWith("ip ")) {
        String ip = line.substring(3);
        ip.trim();
        prefs.putString("serverIP", ip);
        Serial.println("Saved server IP: " + ip);
      } else if (line == "clear") {
        prefs.remove("serverIP");
        Serial.println("Saved server IP cleared.");
      }
    }
    delay(20);
  }
}

// --- Helper Functions ---

// force = true tears the link down first. Used when Wi-Fi still *says* it is
// connected but requests fail (the link goes stale after light sleep).
void ensureWiFiConnected(bool force) {
  if (force || WiFi.status() != WL_CONNECTED) {
    Serial.print("Connecting to Wi-Fi...");

    if (force) {
      WiFi.disconnect(true);   // fully drop the stale link
      delay(200);
    }

    // FIX FOR BOOT LOOP: Ensure STA mode is active, then clear internal state
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    delay(100);

    WiFi.begin(ssid, password);
    unsigned long startAttempt = millis();

    while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 6000) {
      delay(250);
      Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\n[Wi-Fi] Connected! IP: " + WiFi.localIP().toString());
      // The network may have changed, so find the server again
      discoverServer();
    } else {
      Serial.println("\n[Wi-Fi] Connection timed out.");
    }
  }
}

void checkBackendReachable() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[Backend Check] Skipped -- Wi-Fi not connected.");
    return;
  }
  if (serverIP.length() == 0) {
    Serial.println("[Backend Check] No Flask server found on this network.");
    return;
  }

  String pingURL = apiUrl("/api/ping");
  Serial.println("[Backend Check] Pinging " + pingURL + " ...");
  HTTPClient http;
  http.begin(pingURL);
  http.setTimeout(4000);
  int code = http.GET();

  if (code > 0) {
    Serial.printf("[Backend Check] OK -- server responded with code %d\n", code);
    Serial.println("[Backend Check] Reply: " + http.getString());
  } else {
    Serial.printf("[Backend Check] FAILED (%d): %s\n", code, http.errorToString(code).c_str());
    Serial.println("[Backend Check] The device cannot reach the Flask server at " + serverIP);
  }
  http.end();
}

void updateGNSSLocation() {
  Serial.println("Reading GNSS data from A7670E Modem...");
  while (modemSerial.available()) modemSerial.read();

  modemSerial.println("AT+CGPSINFO");
  unsigned long timeout = millis() + 1000;
  String response = "";

  while (millis() < timeout) {
    while (modemSerial.available()) {
      char c = modemSerial.read();
      response += c;
    }
  }

  if (response.indexOf("+CGPSINFO: ,,,,,,,,") != -1) {
    Serial.println("No current satellite fix. Retaining last known coordinates.");
    hasFix = false;
  }
  else if (response.indexOf("+CGPSINFO:") != -1) {
    int startIndex = response.indexOf("+CGPSINFO:") + 10;
    int endIndex = response.indexOf("\r", startIndex);
    if (endIndex != -1) {
      String data = response.substring(startIndex, endIndex);
      data.trim();
      currentCoordinates = data;
      Serial.print("Raw Location: ");
      Serial.println(currentCoordinates);

      if (parseGNSSToDecimal(data, currentLat, currentLng)) {
        hasFix = true;
        Serial.printf("Decimal Degrees -> Lat: %.6f, Lng: %.6f\n", currentLat, currentLng);
      } else {
        hasFix = false;
        Serial.println("Error parsing GNSS string formatting.");
      }
    }
  }
}

bool parseGNSSToDecimal(const String &data, double &outLat, double &outLng) {
  int c1 = data.indexOf(',');
  int c2 = data.indexOf(',', c1 + 1);
  int c3 = data.indexOf(',', c2 + 1);
  int c4 = data.indexOf(',', c3 + 1);
  if (c1 == -1 || c2 == -1 || c3 == -1) return false;

  String latRaw = data.substring(0, c1);
  String latDir = data.substring(c1 + 1, c2);
  String lngRaw = data.substring(c2 + 1, c3);
  String lngDir = (c4 == -1) ? data.substring(c3 + 1) : data.substring(c3 + 1, c4);

  latRaw.trim(); latDir.trim(); lngRaw.trim(); lngDir.trim();
  if (latRaw.length() == 0 || lngRaw.length() == 0) return false;

  double latVal = latRaw.toDouble();
  double lngVal = lngRaw.toDouble();

  double latDeg = floor(latVal / 100.0);
  double latMin = latVal - (latDeg * 100.0);
  double lat = latDeg + (latMin / 60.0);

  double lngDeg = floor(lngVal / 100.0);
  double lngMin = lngVal - (lngDeg * 100.0);
  double lng = lngDeg + (lngMin / 60.0);

  if (latDir == "S") lat = -lat;
  if (lngDir == "W") lng = -lng;

  outLat = lat;
  outLng = lng;
  return true;
}

// Asks the Flask server for a readable place name. Only used for the SOS text
// message; the server does its own geocoding when it stores an alert.
String fetchAddress(double lat, double lng) {
  if (WiFi.status() != WL_CONNECTED || serverIP.length() == 0) return "";

  HTTPClient http;
  String url = apiUrl("/api/geocode") + "?lat=" + String(lat, 6) + "&lng=" + String(lng, 6);
  http.begin(url);
  http.setTimeout(5000);
  int code = http.GET();

  String address = "";
  if (code == 200) {
    String body = http.getString();

    int keyIdx = body.indexOf("\"address\"");
    if (keyIdx != -1) {
      int colonIdx = body.indexOf(':', keyIdx);
      if (colonIdx != -1) {
        int startQuote = body.indexOf('"', colonIdx + 1);
        if (startQuote != -1) {
          int endQuote = body.indexOf('"', startQuote + 1);
          if (endQuote != -1) {
            address = body.substring(startQuote + 1, endQuote);
          }
        }
      }
    }
  }
  http.end();
  return address;
}

String buildSOSMessage() {
  if (!hasEverFixed) {
    return "SOS Alert! Panic button pressed. GPS has not locked yet -- device: " + deviceSerial + ". Please respond immediately.";
  }

  ensureWiFiConnected();
  String address = fetchAddress(currentLat, currentLng);

  String message = "SOS Alert! ";
  if (address.length() > 0) {
    message += address + " ";
  }
  message += "Latitude:" + String(currentLat, 6) + " Longitude:" + String(currentLng, 6);
  return message;
}

void sendSMS(String number, String message) {
  Serial.println("-> Despatching cellular notification...");

  // BUZZER FIX: Trigger it immediately instead of waiting for the SMS to send!
  modemSerial.println(SIMTONE_CMD);

  while (modemSerial.available()) modemSerial.read(); // clear any stale data

  modemSerial.print("AT+CMGS=\"");
  modemSerial.print(number);
  modemSerial.println("\"");
  delay(1000);
  modemSerial.print(message);
  delay(500);
  modemSerial.write(26); // Ctrl+Z, tells the modem the message body is done

  // Wait up to 10s for the modem to confirm the SMS was sent
  unsigned long timeout = millis() + 10000;
  String response = "";
  bool confirmed = false;

  while (millis() < timeout) {
    while (modemSerial.available()) {
      char c = modemSerial.read();
      response += c;
    }
    if (response.indexOf("+CMGS:") != -1 || response.indexOf("OK") != -1) {
      confirmed = true;
      break;
    }
    if (response.indexOf("ERROR") != -1) {
      break;
    }
  }

  if (confirmed) {
    Serial.println("-> Cellular transmission confirmed. SMS Sent.");
  } else {
    Serial.println("-> WARNING: SMS send not confirmed within timeout.");
    Serial.println("-> Modem response was: " + response);
  }
}

// Escapes quotes/backslashes/newlines so the JSON body is always valid.
String jsonEscape(const String &s) {
  String out = "";
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (c == '\n') out += "\\n";
    else if (c == '\r') { /* skip */ }
    else out += c;
  }
  return out;
}

String buildPayload() {
  String p = "{";
  p += "\"device_serial\":\"" + jsonEscape(deviceSerial) + "\",";
  if (hasEverFixed) {
    // Last known good coordinates (kept even if the current read has no fix)
    p += "\"lat\":" + String(currentLat, 6) + ",";
    p += "\"lng\":" + String(currentLng, 6) + ",";
  } else {
    // Never sent 0,0 -- that would plot a fake location in the ocean
    p += "\"lat\":null,\"lng\":null,";
  }
  p += "\"fix\":" + String(hasFix ? "true" : "false") + ",";
  p += "\"raw\":\"" + jsonEscape(currentCoordinates) + "\"";
  p += "}";
  return p;
}

void sendToFlaskBackend(const char* path) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("-> Network error: Wi-Fi dropped. Could not reach Flask.");
    return;
  }
  if (serverIP.length() == 0 && !discoverServer()) {
    Serial.println("-> No Flask server found on this network. Skipping.");
    return;
  }

  String payload = buildPayload();

  // Two tries: if the first fails, the server's IP may have changed, so look again.
  for (int attempt = 0; attempt < 2; attempt++) {
    String url = apiUrl(path);
    Serial.println("-> Transmitting JSON packet to " + url);

    HTTPClient http;
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000);
    int code = http.POST(payload);
    http.end();

    if (code > 0) {
      Serial.print("-> Backend responded with Code: ");
      Serial.println(code);
      return;
    }

    Serial.print("-> Error connecting to backend: ");
    Serial.println(code);

    serverIP = "";
    if (attempt == 0) {
      ensureWiFiConnected(true);   // rebuild the link (also re-discovers the server)
      if (serverIP.length() == 0) return;
    }
  }
}

bool sendATCommand(String command, String expectedResponse, unsigned long timeout) {
  while (modemSerial.available()) modemSerial.read();
  modemSerial.println(command);
  unsigned long time = millis();
  String response = "";

  while ((time + timeout) > millis()) {
    while (modemSerial.available()) {
      char c = modemSerial.read();
      response += c;
    }
    if (response.indexOf(expectedResponse) != -1) {
      return true;
    }
  }
  return false;
}