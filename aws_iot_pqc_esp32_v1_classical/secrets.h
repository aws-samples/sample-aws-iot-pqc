/*
 * secrets.h - Configuration for v1.0 Classical TLS firmware
 *
 * This file is identical to the v2.0 PQC firmware's secrets.h.
 * Both versions use the same WiFi, endpoint, and certificates.
 * The only difference is the TLS library used (mbedTLS vs wolfSSL).
 */

#ifndef SECRETS_H
#define SECRETS_H

// ─── WiFi Configuration ─────────────────────────────────────────────────────
#define WIFI_SSID     "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// ─── Logging Configuration ──────────────────────────────────────────────────
//   0 = NONE, 1 = ERROR, 2 = WARN, 3 = INFO, 4 = DEBUG, 5 = VERBOSE
#define LOG_LEVEL  3

// ─── AWS IoT Core Configuration ─────────────────────────────────────────────
#define AWS_IOT_ENDPOINT "xxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com"
#define AWS_IOT_PORT     8883
#define THINGNAME        "my-esp32-pqc-thing"

// ─── Amazon Root CA 1 ───────────────────────────────────────────────────────
static const char AWS_CERT_CA[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
REPLACE_WITH_YOUR_AMAZON_ROOT_CA_1
-----END CERTIFICATE-----
)EOF";

// ─── Device Certificate ─────────────────────────────────────────────────────
static const char AWS_CERT_CRT[] PROGMEM = R"KEY(
-----BEGIN CERTIFICATE-----
REPLACE_WITH_YOUR_DEVICE_CERTIFICATE
-----END CERTIFICATE-----
)KEY";

// ─── Device Private Key ─────────────────────────────────────────────────────
static const char AWS_CERT_PRIVATE[] PROGMEM = R"KEY(
-----BEGIN RSA PRIVATE KEY-----
REPLACE_WITH_YOUR_DEVICE_PRIVATE_KEY
-----END RSA PRIVATE KEY-----
)KEY";

#endif // SECRETS_H
