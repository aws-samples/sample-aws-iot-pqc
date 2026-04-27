/*
 * secrets.h - Configuration and credentials for AWS IoT Core PQC connection
 *
 * INSTRUCTIONS:
 * 1. Replace the WiFi credentials with your network details.
 * 2. Replace the AWS IoT endpoint with your account's endpoint.
 *    Find it in the AWS IoT Console under Settings, or run:
 *      aws iot describe-endpoint --endpoint-type iot:Data-ATS
 * 3. Paste your device certificate and private key from the AWS IoT
 *    Console (when you create a Thing and download the certificates).
 * 4. The Amazon Root CA 1 is provided below. If you need a different
 *    root CA, download it from:
 *      https://www.amazontrust.com/repository/
 *
 * SECURITY NOTE:
 *   Never commit this file with real credentials to version control.
 *   The certificates below are placeholders only.
 */

#ifndef SECRETS_H
#define SECRETS_H

// ─── WiFi Configuration ─────────────────────────────────────────────────────
#define WIFI_SSID     "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// ─── AWS IoT Core Configuration ─────────────────────────────────────────────
#define AWS_IOT_ENDPOINT "xxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com"
#define AWS_IOT_PORT     8883
#define THINGNAME        "my-esp32-pqc-thing"

// MQTT topics
#define AWS_IOT_PUBLISH_TOPIC   "esp32/pqc/pub"
#define AWS_IOT_SUBSCRIBE_TOPIC "esp32/pqc/sub"

// ─── Amazon Root CA 1 ───────────────────────────────────────────────────────
// Download from: https://www.amazontrust.com/repository/AmazonRootCA1.pem
static const char AWS_CERT_CA[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL
MAkGA1UEBhMCVVMxDwYNBgQKEwZBbWF6b24xGTAXBgNVBAMTEEFtYXpvbiBSb290
IENBIDEwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCwKFMajpIBbqTq
REPLACE_WITH_YOUR_AMAZON_ROOT_CA_1
-----END CERTIFICATE-----
)EOF";

// ─── Device Certificate ─────────────────────────────────────────────────────
// From AWS IoT Console: Thing -> Certificates -> Download
static const char AWS_CERT_CRT[] PROGMEM = R"KEY(
-----BEGIN CERTIFICATE-----
REPLACE_WITH_YOUR_DEVICE_CERTIFICATE
-----END CERTIFICATE-----
)KEY";

// ─── Device Private Key ─────────────────────────────────────────────────────
// From AWS IoT Console: Thing -> Certificates -> Download
static const char AWS_CERT_PRIVATE[] PROGMEM = R"KEY(
-----BEGIN RSA PRIVATE KEY-----
REPLACE_WITH_YOUR_DEVICE_PRIVATE_KEY
-----END RSA PRIVATE KEY-----
)KEY";

#endif // SECRETS_H
