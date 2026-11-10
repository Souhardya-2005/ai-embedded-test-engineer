
#include <Arduino.h>
#include <string.h>
#include <ctype.h>
#include <math.h>

// AI-Powered Embedded Test Engineer
// Hardware: Arduino UNO R3 + documented LED test fixture

const uint8_t DRIVE_PIN = 8;       // D8 drives the LED test circuit
const uint8_t SENSE_INPUT = A0;    // Before the 330-ohm resistor
const uint8_t SENSE_LED = A1;      // LED-anode node, after the resistor

const float ADC_REFERENCE_V = 5.0f; // Nominal only; calibration comes later
const float SERIES_RESISTOR_OHM = 330.0f;
const float PULLDOWN_RESISTOR_OHM = 100000.0f;

const uint8_t SAMPLE_COUNT = 16;
const unsigned long SETTLE_MS = 5;
const size_t MAX_LINE_LENGTH = 80;

char inputLine[MAX_LINE_LENGTH];
size_t inputLength = 0;
bool lineOverflow = false;

// Average ADC samples to reduce random measurement noise.
float readAverageADC(uint8_t pin) {
  unsigned long total = 0;

  // Discard the first conversion after switching ADC channels.
  analogRead(pin);

  for (uint8_t i = 0; i < SAMPLE_COUNT; i++) {
    total += analogRead(pin);
  }

  return (float)total / SAMPLE_COUNT;
}

float adcToVoltage(float adcValue) {
  return adcValue * ADC_REFERENCE_V / 1023.0f;
}

void printError(const char* message) {
  Serial.print(F("{\"ok\":false,\"error\":\""));
  Serial.print(message);
  Serial.println(F("\"}"));
}

void printReady() {
  Serial.println(
    F("{\"ok\":true,\"event\":\"ready\","
      "\"firmware\":\"led_fixture_v1\"}")
  );
}

// Extract the string value of the JSON "cmd" field.
// This is a small command parser, not a general-purpose JSON parser.
bool extractCommand(const char* line, char* command,
                    size_t commandSize) {
  const char* key = strstr(line, "\"cmd\"");
  if (key == NULL) return false;

  const char* colon = strchr(key + 5, ':');
  if (colon == NULL) return false;

  const char* p = colon + 1;

  while (*p && isspace((unsigned char)*p)) {
    p++;
  }

  if (*p != '"') return false;
  p++;

  size_t i = 0;

  while (*p && *p != '"') {
    // Reject escapes rather than interpreting ambiguous input.
    if (*p == '\\') return false;
    if (i + 1 >= commandSize) return false;

    command[i++] = *p++;
  }

  if (*p != '"') return false;

  command[i] = '\0';
  return i > 0;
}

void handlePing() {
  Serial.println(
    F("{\"ok\":true,\"type\":\"pong\","
      "\"firmware\":\"led_fixture_v1\","
      "\"board\":\"Arduino_UNO_R3\"}")
  );
}

void handleLEDTest() {
  // Establish a known low state before enabling the test.
  digitalWrite(DRIVE_PIN, LOW);
  delay(SETTLE_MS);

  // Drive the test circuit through its 330-ohm resistor.
  digitalWrite(DRIVE_PIN, HIGH);
  delay(SETTLE_MS);

  // Measure the voltage on each side of the series resistor.
  float rawA0 = readAverageADC(SENSE_INPUT);
  float rawA1 = readAverageADC(SENSE_LED);

  float voltageA0 = adcToVoltage(rawA0);
  float voltageA1 = adcToVoltage(rawA1);

  // Calculate current using the nominal resistor values.
  float seriesCurrentMA =
    (voltageA0 - voltageA1) / SERIES_RESISTOR_OHM * 1000.0f;

  float pulldownCurrentMA =
    voltageA1 / PULLDOWN_RESISTOR_OHM * 1000.0f;

  float ledCurrentMA = seriesCurrentMA - pulldownCurrentMA;

  // Small negative estimates can arise from ADC quantization/noise.
  // Preserve the raw voltages, but do not report negative current.
  if (seriesCurrentMA < 0.0f) seriesCurrentMA = 0.0f;
  if (ledCurrentMA < 0.0f) ledCurrentMA = 0.0f;

  // Return the drive to a safe inactive state.
 
  // Provisional local check for a typical red LED.
  // The Python backend remains responsible for the official verdict.
  bool ledPass =
      (ledCurrentMA >= 2.0f && ledCurrentMA <= 12.0f) &&
      (voltageA1 >= 1.4f && voltageA1 <= 2.5f);

  // Keep the LED ON after a passing test; otherwise turn it OFF.
  digitalWrite(DRIVE_PIN, ledPass ? HIGH : LOW);

  // The backend will apply calibrated PASS/FAIL limits.
  // These hints are not definitive fault diagnoses.
  const char* hint = "review_measurements";

  if (voltageA1 < 0.5f && ledCurrentMA > 2.0f) {
    hint = "possible_short_like_behavior";
  } else if (voltageA1 > 4.0f && ledCurrentMA < 0.5f) {
    hint = "possible_open_like_behavior";
  } else if (ledCurrentMA >= 0.5f && voltageA1 >= 0.8f &&
             voltageA1 <= 3.8f) {
    hint = "current_detected";
  }

  // One JSON object per line; no terminal formatting is required.
  Serial.print(F("{\"ok\":true,\"type\":\"led_measurement\""));

  Serial.print(F(",\"adc_a0\":"));
  Serial.print(rawA0, 2);

  Serial.print(F(",\"adc_a1\":"));
  Serial.print(rawA1, 2);

  Serial.print(F(",\"voltage_a0_v\":"));
  Serial.print(voltageA0, 4);

  Serial.print(F(",\"voltage_a1_v\":"));
  Serial.print(voltageA1, 4);

  Serial.print(F(",\"series_current_ma\":"));
  Serial.print(seriesCurrentMA, 4);

  Serial.print(F(",\"led_current_ma\":"));
  Serial.print(ledCurrentMA, 4);

  Serial.print(F(",\"hint\":\""));
  Serial.print(hint);
  Serial.println(F("\"}"));
}

void processLine(char* line) {
  // Remove leading whitespace.
  while (*line && isspace((unsigned char)*line)) {
    line++;
  }

  if (*line == '\0') return;

  char command[24];

  if (!extractCommand(line, command, sizeof(command))) {
    printError("invalid_command_format");
    return;
  }

  if (strcmp(command, "ping") == 0) {
    handlePing();
  } else if (strcmp(command, "test_led") == 0) {
    handleLEDTest();
  } else {
    printError("unsupported_command");
  }
}

void setup() {
  // Keep the output low before beginning any measurement.
  digitalWrite(DRIVE_PIN, LOW);
  pinMode(DRIVE_PIN, OUTPUT);

  pinMode(SENSE_INPUT, INPUT);
  pinMode(SENSE_LED, INPUT);

  Serial.begin(115200);

  // USB serial initialization can take time on some host setups.
  delay(200);

  printReady();
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      if (lineOverflow) {
        printError("command_too_long");
      } else {
        inputLine[inputLength] = '\0';
        processLine(inputLine);
      }

      inputLength = 0;
      lineOverflow = false;
      continue;
    }

    if (!lineOverflow) {
      if (inputLength < MAX_LINE_LENGTH - 1) {
        inputLine[inputLength++] = c;
      } else {
        lineOverflow = true;
      }
    }
  }
}
