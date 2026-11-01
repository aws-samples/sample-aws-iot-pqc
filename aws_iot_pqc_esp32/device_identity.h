// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// device_identity.h — who this device is.
//
// The Thing name comes from the chip's MAC address. Keys, certificates, CA
// roots and WiFi credentials come from the "certs" NVS partition, which the
// Builder-Pi writes at registration and OTA never touches — so the same app
// image runs on every device and an OTA image carries no secrets.

#ifndef DEVICE_IDENTITY_H
#define DEVICE_IDENTITY_H

#include <stddef.h>
#include <stdint.h>

// A DER blob read from NVS. Kept for the life of the device: the TLS context
// and every OTA signature check use them.
struct IdBlob {
    uint8_t* data;
    size_t   len;
};

class DeviceIdentity {
public:
    // Derive the Thing name ("esp32-XXYYZZ") from the factory MAC. Call first.
    static void initFromMac();

    // Load keys, certs and WiFi from the "certs" NVS partition. Returns false
    // (after logging why) if anything is missing, malformed, or issued for a
    // different chip; the caller must then not start WiFi/MQTT/OTA.
    static bool loadFromNvs();

    // Hash the running app image once at boot (dashboard integrity badge).
    static void computeRunningFirmwareSha256();

    static const char* thingName()       { return s_thingName; }
    static const char* wifiSsid()        { return s_wifiSsid; }
    static const char* wifiPsk()         { return s_wifiPsk; }
    static const char* firmwareSha256()  { return s_firmwareSha256; }

    // Both CA roots: broker trust (one of them) and OTA signer trust (both).
    static const IdBlob& caClassical()   { return s_caCls; }
    static const IdBlob& caMlDsa()       { return s_caMldsa; }
    // This variant's broker credentials (see ID_TLS_* in variant_config.h).
    static const IdBlob& tlsCa()         { return *s_tlsCa; }
    static const IdBlob& tlsCert()       { return s_tlsCert; }
    static const IdBlob& tlsKey()        { return s_tlsKey; }

private:
    static char          s_thingName[32];
    static char          s_wifiSsid[33];   // 802.11 max 32 + NUL
    static char          s_wifiPsk[65];    // WPA2 max 64 + NUL
    static char          s_firmwareSha256[65];
    static IdBlob        s_caCls;
    static IdBlob        s_caMldsa;
    static IdBlob        s_tlsCert;
    static IdBlob        s_tlsKey;
    static const IdBlob* s_tlsCa;
};

#endif  // DEVICE_IDENTITY_H
