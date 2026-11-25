#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>     // Required for light sleep features
#include <driver/gpio.h>   // Required for GPIO wake-up configurations

// ============================================================================
// WI-FI CREDENTIALS
// ============================================================================
const char* ssid = "FAB_LAB";
const char* password = "researchhub2020";

// ============================================================================
// BACKEND BASE URL
// ============================================================================
const String baseURL = "http://dansoy.pythonanywhere.com";

const String pingURL      = baseURL + "/api/ping";
const String triggerURL   = baseURL + "/api/trigger-hardware";
const String telemetryURL = baseURL + "/api/iot/telemetry";
const String geocodeURL   = baseURL + "/api/geocode";

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
// Changed last value from 5000 to 1000 so the beep only lasts 1 second
const String SIMTONE_CMD = "AT+SIMTONE=1,3000,5000,0,5000";

// --- Modem Object ---
HardwareSerial modemSerial(1);

// --- Global Variables ---
String currentCoordinates = "Waiting for initial GPS lock...";
const unsigned long gnssInterval = 10000; // Sleep interval (10 seconds)

// Function Declarations
void ensureWiFiConnected();
void checkBackendReachable();
void updateGNSSLocation();
bool parseGNSSToDecimal(const String &data, double &outLat, double &outLng);
String fetchAddress(double lat, double lng);
String buildSOSMessage();
void sendSMS(String number, String message);
void sendToFlaskBackend(String targetURL);
bool sendATCommand(String command, String expectedResponse, unsigned long timeout);

void setup() {
  Serial.begin(115200);
  delay(1000);

  // FIX FOR BOOT LOOP: Initialize Wi-Fi state and cap TX power to prevent brownout resets
  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);

  Serial.println("\n--- ESP32 Starting Up (Direct IoT Node Mode) ---");

  // Configure Button Pin
  pinMode(buttonPin, INPUT_PULLUP);

  // Configure GPIO Wakeup for Light Sleep
  esp_sleep_enable_gpio_wakeup();
  gpio_wakeup_enable((gpio_num_t)buttonPin, GPIO_INTR_LOW_LEVEL);

  // Initial Wi-Fi Connection
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

    // BUZZER FIX: Fire the AT command instantly before any GPS/Network delays
    modemSerial.println(SIMTONE_CMD);

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
    sendToFlaskBackend(triggerURL);

  } else {
    // Regular 10-second timer wake up
    Serial.println("\n[WAKEUP] Routine Periodic Telemetry Check...");
    updateGNSSLocation();
    if (hasFix) hasEverFixed = true;

    // Direct background coordinates straight to the backend's TELEMETRY route
    ensureWiFiConnected();
    sendToFlaskBackend(telemetryURL);
  }

  // --- PREPARE FOR LIGHT SLEEP ---
  Serial.println("Entering Light Sleep mode... GNSS remains active.");
  Serial.flush(); // Ensure serial logs are fully sent before pausing the CPU clock

  // Set timer wakeup interval
  esp_sleep_enable_timer_wakeup(gnssInterval * 1000);

  // Execute light sleep
  esp_light_sleep_start();
}

// --- Helper Functions ---

void ensureWiFiConnected() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.print("Connecting to Wi-Fi...");
    
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

  Serial.println("[Backend Check] Pinging " + pingURL + " ...");
  HTTPClient http;
  http.begin(pingURL);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.setTimeout(4000);
  int code = http.GET();

  if (code > 0) {
    Serial.printf("[Backend Check] OK -- server responded with code %d\n", code);
    Serial.println("[Backend Check] Reply: " + http.getString());
  } else {
    Serial.printf("[Backend Check] FAILED (%d): %s\n", code, http.errorToString(code).c_str());
    Serial.println("[Backend Check] The device cannot reach the Flask server at " + baseURL);
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

String fetchAddress(double lat, double lng) {
  if (WiFi.status() != WL_CONNECTED) return "";

  HTTPClient http;
  String url = geocodeURL + "?lat=" + String(lat, 6) + "&lng=" + String(lng, 6);
  http.begin(url);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
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

void sendToFlaskBackend(String targetURL) {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("-> Transmitting JSON telemetry packet to " + targetURL);
    HTTPClient http;
    http.begin(targetURL);
    http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    http.addHeader("Content-Type", "application/json");

    String locName = hasFix ? fetchAddress(currentLat, currentLng) : "Location unavailable, no GNSS fix";

    String jsonPayload = "{";
    jsonPayload += "\"device_serial\":\"" + deviceSerial + "\",";
    jsonPayload += "\"lat\":" + String(currentLat, 6) + ",";
    jsonPayload += "\"lng\":" + String(currentLng, 6) + ",";
    jsonPayload += "\"fix\":" + String(hasFix ? "true" : "false") + ",";
    jsonPayload += "\"raw\":\"" + currentCoordinates + "\",";
    jsonPayload += "\"address\":\"" + locName + "\"";
    jsonPayload += "}";

    int httpResponseCode = http.POST(jsonPayload);

    if (httpResponseCode > 0) {
      Serial.print("-> Backend responded with Code: ");
      Serial.println(httpResponseCode);
    } else {
      Serial.print("-> Error connecting to backend: ");
      Serial.println(httpResponseCode);
    }
    http.end();
  } else {
    Serial.println("-> Network error: Wi-Fi dropped. Could not reach Flask.");
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