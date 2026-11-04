// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// pqc_tls_client.h — wolfSSL TLS 1.3 under the Arduino Client interface.
//
// WolfSSLClient wraps a plain WiFiClient: WiFiClient carries the TCP bytes and
// wolfSSL does TLS 1.3 on top through custom I/O callbacks. On the PQC
// variants it requests the hybrid SecP256r1MLKEM768 key share (see
// VARIANT_PQC_KEX). MQTTClient uses it like any other Client.

#ifndef PQC_TLS_CLIENT_H
#define PQC_TLS_CLIENT_H

#include "app_config.h"
#include <Client.h>

// I/O context for the wolfSSL callbacks: the TCP socket to read and write.
typedef struct {
    WiFiClient* client;
} wolfssl_io_ctx_t;

// wolfSSL custom I/O callbacks over an Arduino WiFiClient. Also used by the
// OTA updater's S3 connections.
int wolfssl_send_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx);
int wolfssl_recv_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx);

class WolfSSLClient : public Client {
public:
    WolfSSLClient();
    ~WolfSSLClient();

    // Create the shared TLS 1.3 context with this device's broker credentials
    // from NVS (DeviceIdentity). Call once, after the clock is set.
    static bool setupContext();

    // Negotiated parameters of the last successful handshake (telemetry).
    static const char* tlsVersion()   { return s_tlsVersion; }
    static const char* tlsCipher()    { return s_tlsCipher; }
    static const char* keyExchange()  { return s_tlsKeyExchange; }

    // Arduino Client interface
    int connect(IPAddress ip, uint16_t port) override;
    int connect(const char* host, uint16_t port) override;
    size_t write(uint8_t b) override;
    size_t write(const uint8_t* buf, size_t size) override;
    int available() override;
    int read() override;
    int read(uint8_t* buf, size_t size) override;
    int peek() override;
    void flush() override;
    void stop() override;
    uint8_t connected() override;
    operator bool() override;

private:
    WiFiClient       _tcpClient;
    WOLFSSL*         _ssl;
    wolfssl_io_ctx_t _ioCtx;
    bool             _connected;
    bool             _pqcRequested;

    static WOLFSSL_CTX* s_ctx;   // shared, thread-safe after init
    static char s_tlsVersion[32];
    static char s_tlsCipher[64];
    static char s_tlsKeyExchange[64];
};

#endif  // PQC_TLS_CLIENT_H
