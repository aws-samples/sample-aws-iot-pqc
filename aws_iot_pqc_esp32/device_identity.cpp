// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "device_identity.h"
#include <Preferences.h>   // device identity from the "certs" NVS partition

// ═══════════════════════════════════════════════════════════════════════════
//  Device identity from the "certs" NVS partition
// ═══════════════════════════════════════════════════════════════════════════
// Per-device keys, certificates, CA roots and WiFi credentials are NOT compiled
// into this image. The Builder-Pi writes them over USB into a dedicated NVS
// partition labelled "certs" (0x721000, 64 KB, see partitions_pqc_workshop.csv)
// that OTA never touches. So the same app image runs on every device, and an
// OTA image carries no secrets. Namespace "devid" (format 1):
//   fmt (u8)                      layout version, must be ID_FORMAT
//   serial, wifi_ssid, wifi_psk   strings
//   ca_cls, ca_mldsa              CA roots (DER): broker trust + OTA signer trust
//   crt_cls, key_cls              classical device cert + key (DER)
//   crt_mldsa, key_mldsa          ML-DSA-44 device cert + seed-only PKCS#8 key
// The variant picks its broker credentials with ID_TLS_CA / ID_TLS_CERT /
// ID_TLS_KEY (variant_config.h).
#define ID_PARTITION   "certs"
#define ID_NAMESPACE   "devid"
#define ID_FORMAT      1

char          DeviceIdentity::s_thingName[32]      = {0};  // "esp32-XXYYZZ"
char          DeviceIdentity::s_wifiSsid[33]       = {0};
char          DeviceIdentity::s_wifiPsk[65]        = {0};
char          DeviceIdentity::s_firmwareSha256[65] = {0};
IdBlob        DeviceIdentity::s_caCls              = {nullptr, 0};
IdBlob        DeviceIdentity::s_caMldsa            = {nullptr, 0};
IdBlob        DeviceIdentity::s_tlsCert            = {nullptr, 0};
IdBlob        DeviceIdentity::s_tlsKey             = {nullptr, 0};
const IdBlob* DeviceIdentity::s_tlsCa              = &DeviceIdentity::s_caCls;

// ─── Device Identity from MAC Address ───────────────────────────────────────
// Derives a unique Thing name from the ESP32's factory-burned MAC address.
// Format: "esp32-XXYYZZ" where XXYYZZ are the last 3 bytes of the MAC.
// This allows a single firmware binary to run on any device.
void DeviceIdentity::initFromMac() {
    uint64_t mac = ESP.getEfuseMac();
    uint8_t* macBytes = (uint8_t*)&mac;
    snprintf(s_thingName, sizeof(s_thingName), "esp32-%02X%02X%02X",
             macBytes[3], macBytes[4], macBytes[5]);
    ESP_LOGI(TAG_MAIN, "Device identity: %s (MAC-derived)", s_thingName);
}

static bool idReadBlob(Preferences& p, const char* key, IdBlob& blob) {
    size_t n = p.getBytesLength(key);
    if (n == 0) {
        ESP_LOGE(TAG_MAIN, "[Setup] Identity entry '%s' is missing", key);
        return false;
    }
    blob.data = (uint8_t*)malloc(n);
    if (!blob.data) {
        ESP_LOGE(TAG_MAIN, "[Setup] Out of memory reading '%s' (%u B)", key, (unsigned)n);
        return false;
    }
    blob.len = p.getBytes(key, blob.data, n);
    if (blob.len != n) {
        ESP_LOGE(TAG_MAIN, "[Setup] Short read of '%s' (%u of %u B)", key,
                 (unsigned)blob.len, (unsigned)n);
        return false;
    }
    return true;
}

// Reads a NUL-terminated string. getString returns 0 if the entry is missing
// or does not fit in buf.
static bool idReadString(Preferences& p, const char* key, char* buf, size_t cap) {
    if (p.getString(key, buf, cap) == 0) {
        ESP_LOGE(TAG_MAIN, "[Setup] Identity entry '%s' is missing or too long", key);
        return false;
    }
    return true;
}

// Requires the Thing name (initFromMac).
bool DeviceIdentity::loadFromNvs() {
    Preferences p;
    if (!p.begin(ID_NAMESPACE, true /* read-only */, ID_PARTITION)) {
        ESP_LOGE(TAG_MAIN, "[Setup] Cannot open NVS partition '%s' namespace '%s'",
                 ID_PARTITION, ID_NAMESPACE);
        return false;
    }
    bool ok = true;
    uint8_t fmt = p.getUChar("fmt", 0);
    if (fmt != ID_FORMAT) {
        ESP_LOGE(TAG_MAIN, "[Setup] Identity format %u, expected %u", fmt, ID_FORMAT);
        ok = false;
    }
    char serial[17] = {0};
    ok = ok && idReadString(p, "serial", serial, sizeof(serial));
    // The identity was issued for one serial. Refuse it on any other chip, so a
    // certs image flashed onto the wrong board can never impersonate a device.
    if (ok && strcmp(serial, s_thingName + strlen("esp32-")) != 0) {
        ESP_LOGE(TAG_MAIN, "[Setup] Identity is for serial %s but this chip is %s",
                 serial, s_thingName);
        ok = false;
    }
    ok = ok && idReadString(p, "wifi_ssid", s_wifiSsid, sizeof(s_wifiSsid));
    ok = ok && idReadString(p, "wifi_psk", s_wifiPsk, sizeof(s_wifiPsk));
    ok = ok && idReadBlob(p, "ca_cls", s_caCls);
    ok = ok && idReadBlob(p, "ca_mldsa", s_caMldsa);
    ok = ok && idReadBlob(p, ID_TLS_CERT, s_tlsCert);
    ok = ok && idReadBlob(p, ID_TLS_KEY, s_tlsKey);
    p.end();
    if (!ok) return false;

    s_tlsCa = (strcmp(ID_TLS_CA, "ca_mldsa") == 0) ? &s_caMldsa : &s_caCls;
    ESP_LOGI(TAG_MAIN, "[Setup] Device identity loaded from NVS '%s' (serial %s, WiFi '%s')",
             ID_PARTITION, serial, s_wifiSsid);
    ESP_LOGI(TAG_MAIN, "[Setup]   %s %u B, %s %u B; broker trust %s; OTA trust ca_cls %u B + ca_mldsa %u B",
             ID_TLS_CERT, (unsigned)s_tlsCert.len, ID_TLS_KEY, (unsigned)s_tlsKey.len,
             ID_TLS_CA, (unsigned)s_caCls.len, (unsigned)s_caMldsa.len);
    return true;
}

// ─── Running-image SHA-256 (dashboard integrity badge) ──────────────────────
// esp_partition_get_sha256() hashes the actual app-image region of the active
// OTA partition (not the whole partition), so this digest matches the
// firmware.bin the build produced and signed — letting the dashboard tie the
// running image to the exact bytes that were OTA-verified. Best-effort: on any
// error the hash stays empty and telemetry simply omits a real value.
void DeviceIdentity::computeRunningFirmwareSha256() {
    const esp_partition_t* running = esp_ota_get_running_partition();
    if (!running) {
        ESP_LOGW(TAG_MAIN, "running partition not found; firmware sha256 unavailable");
        return;
    }
    uint8_t digest[32] = {0};
    esp_err_t err = esp_partition_get_sha256(running, digest);
    if (err != ESP_OK) {
        ESP_LOGW(TAG_MAIN, "esp_partition_get_sha256 failed: %s", esp_err_to_name(err));
        return;
    }
    for (int i = 0; i < 32; i++) {
        snprintf(s_firmwareSha256 + 2 * i, 3, "%02x", digest[i]);
    }
    ESP_LOGI(TAG_MAIN, "Running image sha256: %s (partition '%s')",
             s_firmwareSha256, running->label);
}
