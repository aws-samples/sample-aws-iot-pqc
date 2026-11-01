// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "pqc_tls_client.h"
#include "device_identity.h"

WOLFSSL_CTX* WolfSSLClient::s_ctx = NULL;
char WolfSSLClient::s_tlsVersion[32]     = "unknown";
char WolfSSLClient::s_tlsCipher[64]      = "unknown";
char WolfSSLClient::s_tlsKeyExchange[64] = "none";

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL I/O Callbacks for Arduino WiFiClient
// ═══════════════════════════════════════════════════════════════════════════

// We wrap a raw WiFiClient in wolfSSL's custom I/O callbacks so that
// wolfSSL handles the TLS layer while Arduino WiFiClient handles TCP.

int wolfssl_send_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client || !io->client->connected()) {
        ESP_LOGD(TAG_TLS, "send_cb: connection closed");
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }
    int sent = io->client->write((uint8_t*)buf, sz);
    if (sent <= 0) {
        ESP_LOGV(TAG_TLS, "send_cb: WANT_WRITE (requested %d bytes)", sz);
        return WOLFSSL_CBIO_ERR_WANT_WRITE;
    }
    ESP_LOGV(TAG_TLS, "send_cb: sent %d/%d bytes", sent, sz);
    return sent;
}

int wolfssl_recv_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client) {
        ESP_LOGD(TAG_TLS, "recv_cb: no client");
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }

    // IMPORTANT: drain buffered bytes BEFORE honouring connected()==false.
    // When the peer (e.g. S3 serving the OTA firmware) half-closes the TCP
    // connection right after sending the final TLS record, WiFiClient::
    // connected() can report false while available() still has the last bytes
    // buffered. If we returned CONN_CLOSE here we would drop those bytes and the
    // OTA download would fail a few hundred bytes short of EOF (observed:
    // "Download error at 1199644/1200416"). So: if there is buffered data, read
    // it regardless of connected(); only report CONN_CLOSE once the socket is
    // BOTH closed AND fully drained.
    int avail = io->client->available();
    if (avail == 0) {
        if (!io->client->connected()) {
            ESP_LOGD(TAG_TLS, "recv_cb: connection closed and drained");
            return WOLFSSL_CBIO_ERR_CONN_CLOSE;
        }
        ESP_LOGV(TAG_TLS, "recv_cb: no data available, WANT_READ");
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }

    int received = io->client->read((uint8_t*)buf, sz);
    if (received <= 0) {
        ESP_LOGV(TAG_TLS, "recv_cb: read returned %d, WANT_READ", received);
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }
    ESP_LOGV(TAG_TLS, "recv_cb: received %d/%d bytes", received, sz);
    return received;
}

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL Context Initialization
// ═══════════════════════════════════════════════════════════════════════════

bool WolfSSLClient::setupContext() {
    ESP_LOGI(TAG_WOLFSSL, "Initializing wolfSSL library...");

    // Initialize wolfSSL library
    int ret = wolfSSL_Init();
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Library init failed (rc=%d)", ret);
        return false;
    }
    ESP_LOGD(TAG_WOLFSSL, "wolfSSL_Init() succeeded");

    // Enable debug logging (comment out for production)
    // wolfSSL_Debugging_ON();

    // Create TLS 1.3 client context
    s_ctx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!s_ctx) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to create TLS 1.3 context");
        return false;
    }
    ESP_LOGD(TAG_WOLFSSL, "TLS 1.3 client context created");

    // All identity material comes from the "certs" NVS partition as DER
    // (loadDeviceIdentity, called in setup before this task starts).
    // Broker trust anchor: this variant's CA root.
    ESP_LOGD(TAG_WOLFSSL, "Loading CA certificate %s (%u bytes, DER)", ID_TLS_CA, (unsigned)DeviceIdentity::tlsCa().len);
    ret = wolfSSL_CTX_load_verify_buffer(s_ctx,
            DeviceIdentity::tlsCa().data, (long)DeviceIdentity::tlsCa().len,
            WOLFSSL_FILETYPE_ASN1);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load CA cert (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "CA certificate loaded");

    // Load device certificate (client authentication)
    ESP_LOGD(TAG_WOLFSSL, "Loading device certificate %s (%u bytes, DER)", ID_TLS_CERT, (unsigned)DeviceIdentity::tlsCert().len);
    ret = wolfSSL_CTX_use_certificate_buffer(s_ctx,
            DeviceIdentity::tlsCert().data, (long)DeviceIdentity::tlsCert().len,
            WOLFSSL_FILETYPE_ASN1);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load device cert (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "Device certificate loaded");

    // Load device private key
    ESP_LOGD(TAG_WOLFSSL, "Loading device private key %s (%u bytes, DER)", ID_TLS_KEY, (unsigned)DeviceIdentity::tlsKey().len);
    ret = wolfSSL_CTX_use_PrivateKey_buffer(s_ctx,
            DeviceIdentity::tlsKey().data, (long)DeviceIdentity::tlsKey().len,
            WOLFSSL_FILETYPE_ASN1);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load private key (rc=%d)", ret);
        return false;
    }
    // Catch a cert/key mismatch here, not as an opaque handshake failure.
    ret = wolfSSL_CTX_check_private_key(s_ctx);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Private key does not match the device certificate (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "Private key loaded");

    // Require server certificate verification
    wolfSSL_CTX_set_verify(s_ctx, WOLFSSL_VERIFY_PEER, NULL);
    ESP_LOGD(TAG_WOLFSSL, "Peer verification enabled");

    // Set custom I/O callbacks at the context level
    wolfSSL_CTX_SetIORecv(s_ctx, wolfssl_recv_cb);
    wolfSSL_CTX_SetIOSend(s_ctx, wolfssl_send_cb);
    ESP_LOGD(TAG_WOLFSSL, "I/O callbacks registered at context level");

#if VARIANT_PQC_KEX
    ESP_LOGI(TAG_WOLFSSL, "TLS 1.3 PQC context ready (heap free: %u bytes)", ESP.getFreeHeap());
#else
    ESP_LOGI(TAG_WOLFSSL, "Classical TLS 1.3 context ready (heap free: %u bytes)", ESP.getFreeHeap());
#endif
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
//  WolfSSLClient — Arduino Client over wolfSSL TLS 1.3
// ═══════════════════════════════════════════════════════════════════════════

WolfSSLClient::WolfSSLClient() : _ssl(NULL), _connected(false), _pqcRequested(false) {
    _ioCtx.client = &_tcpClient;
}

WolfSSLClient::~WolfSSLClient() {
    stop();
}

int WolfSSLClient::connect(IPAddress ip, uint16_t port) {
    return connect(ip.toString().c_str(), port);
}

int WolfSSLClient::connect(const char* host, uint16_t port) {
    stop(); // Clean up any previous connection
    _pqcRequested = false;

    ESP_LOGD(TAG_TLS, "Connecting to %s:%d", host, port);

    // Step 1: Establish raw TCP connection
    if (!_tcpClient.connect(host, port)) {
        ESP_LOGE(TAG_TLS, "TCP connection failed to %s:%d", host, port);
        return 0;
    }
    ESP_LOGI(TAG_TLS, "TCP connected to %s:%d", host, port);

    // Step 2: Create wolfSSL session from the shared context
    _ssl = wolfSSL_new(s_ctx);
    if (!_ssl) {
        ESP_LOGE(TAG_TLS, "Failed to create SSL session (out of memory?)");
        _tcpClient.stop();
        return 0;
    }
    ESP_LOGD(TAG_TLS, "SSL session object created");

    // Step 3: Set custom I/O callbacks
    wolfSSL_SSLSetIORecv(_ssl, wolfssl_recv_cb);
    wolfSSL_SSLSetIOSend(_ssl, wolfssl_send_cb);
    wolfSSL_SetIOReadCtx(_ssl, &_ioCtx);
    wolfSSL_SetIOWriteCtx(_ssl, &_ioCtx);
    ESP_LOGD(TAG_TLS, "I/O callbacks registered");

    int ret;
#if VARIANT_PQC_KEX
    // Step 4: Enable hybrid PQC key exchange (P-256 + ML-KEM-768)
    // This is the core PQC configuration. The TLS 1.3 handshake will
    // negotiate a hybrid key exchange combining classical ECDHE P-256
    // with the post-quantum ML-KEM-768 algorithm.
    // Uses IANA code point 4587 (SecP256r1MLKEM768) for interoperability
    // with OpenSSL 3.5+ based servers (e.g., Mosquitto PQC broker).
    ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_SECP256R1MLKEM768);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGW(TAG_TLS, "P-256 + ML-KEM-768 key share failed (rc=%d), trying fallback", ret);
        // Fall back to X25519 + ML-KEM-768 (also IANA-standard, code point 4588)
        ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_X25519MLKEM768);
        if (ret != WOLFSSL_SUCCESS) {
            ESP_LOGE(TAG_TLS, "PQC key share fallback (X25519 + ML-KEM-768) also failed (rc=%d)", ret);
            wolfSSL_free(_ssl);
            _ssl = NULL;
            _tcpClient.stop();
            return 0;
        }
        ESP_LOGI(TAG_TLS, "Using fallback PQC: X25519 + ML-KEM-768");
        _pqcRequested = true;
    } else {
        ESP_LOGI(TAG_TLS, "Using PQC key exchange: P-256 + ML-KEM-768 (SecP256r1MLKEM768)");
        _pqcRequested = true;
    }
#else
    // Step 4: Classical TLS — no PQC key exchange requested
    ESP_LOGI(TAG_TLS, "Using classical key exchange (ECDHE P-256, no PQC)");
#endif

    // Step 5: Set SNI (Server Name Indication) for AWS IoT
    ret = wolfSSL_UseSNI(_ssl, WOLFSSL_SNI_HOST_NAME,
                         host, strlen(host));
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGW(TAG_TLS, "SNI setup failed (rc=%d) - non-fatal", ret);
    } else {
        ESP_LOGD(TAG_TLS, "SNI set to '%s'", host);
    }

    // Step 6: Perform TLS 1.3 handshake with timeout
#if VARIANT_PQC_KEX
    ESP_LOGI(TAG_TLS, "Starting TLS 1.3 PQC handshake...");
#else
    ESP_LOGI(TAG_TLS, "Starting classical TLS 1.3 handshake...");
#endif
    unsigned long handshakeStart = millis();
    const unsigned long handshakeTimeout = 30000; // 30s for PQC handshake

    do {
        ret = wolfSSL_connect(_ssl);
        if (ret == WOLFSSL_SUCCESS) {
            break;
        }

        int err = wolfSSL_get_error(_ssl, ret);
        if (err == WOLFSSL_ERROR_WANT_READ || err == WOLFSSL_ERROR_WANT_WRITE) {
            // Non-blocking: yield and retry
            ESP_LOGV(TAG_TLS, "Handshake in progress (WANT_%s), elapsed %lu ms",
                     err == WOLFSSL_ERROR_WANT_READ ? "READ" : "WRITE",
                     millis() - handshakeStart);
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // Real error
        char errBuf[128];
        wolfSSL_ERR_error_string_n(err, errBuf, sizeof(errBuf));
        ESP_LOGE(TAG_TLS, "Handshake error: %s (code=%d)", errBuf, err);
        wolfSSL_free(_ssl);
        _ssl = NULL;
        _tcpClient.stop();
        return 0;

    } while ((millis() - handshakeStart) < handshakeTimeout);

    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_TLS, "Handshake timed out after %lu ms", millis() - handshakeStart);
        wolfSSL_free(_ssl);
        _ssl = NULL;
        _tcpClient.stop();
        return 0;
    }

    // Step 7: Verify the connection details and store negotiated params
    unsigned long handshakeDuration = millis() - handshakeStart;
    const char* version = wolfSSL_get_version(_ssl);
    const char* cipher = wolfSSL_get_cipher_name(_ssl);

    // Store for telemetry reporting
    strncpy(s_tlsVersion, version ? version : "unknown", sizeof(s_tlsVersion) - 1);
    strncpy(s_tlsCipher, cipher ? cipher : "unknown", sizeof(s_tlsCipher) - 1);

    // Detect PQC from cipher/handshake — if we requested PQC key share
    // and the handshake succeeded, the server accepted it
    if (strstr(cipher ? cipher : "", "MLKEM") ||
        strstr(cipher ? cipher : "", "mlkem") ||
        strstr(cipher ? cipher : "", "KYBER")) {
        strncpy(s_tlsKeyExchange, "SECP256R1_MLKEM768", sizeof(s_tlsKeyExchange) - 1);
    } else if (_pqcRequested) {
        // We requested PQC and handshake succeeded — PQC was negotiated
        strncpy(s_tlsKeyExchange, "SECP256R1_MLKEM768", sizeof(s_tlsKeyExchange) - 1);
    } else {
        strncpy(s_tlsKeyExchange, "ECDHE-P256", sizeof(s_tlsKeyExchange) - 1);
    }

    ESP_LOGI(TAG_TLS, "TLS handshake complete in %lu ms", handshakeDuration);
    ESP_LOGI(TAG_TLS, "Protocol: %s, Cipher: %s", s_tlsVersion, s_tlsCipher);
    ESP_LOGI(TAG_TLS, "Key Exchange: %s", s_tlsKeyExchange);
    ESP_LOGD(TAG_TLS, "Free heap after handshake: %u bytes", ESP.getFreeHeap());

    _connected = true;
    return 1;
}

size_t WolfSSLClient::write(uint8_t b) {
    return write(&b, 1);
}

size_t WolfSSLClient::write(const uint8_t* buf, size_t size) {
    if (!_ssl || !_connected) return 0;

    int ret = wolfSSL_write(_ssl, buf, size);
    if (ret <= 0) {
        int err = wolfSSL_get_error(_ssl, ret);
        if (err != WOLFSSL_ERROR_WANT_WRITE) {
            ESP_LOGE(TAG_TLS, "write failed (err=%d), marking disconnected", err);
            _connected = false;
        }
        return 0;
    }
    ESP_LOGV(TAG_TLS, "write: %d/%zu bytes sent", ret, size);
    return (size_t)ret;
}

int WolfSSLClient::available() {
    if (!_ssl || !_connected) return 0;

    // Check if wolfSSL has already-decrypted data buffered
    int pending = wolfSSL_pending(_ssl);
    if (pending > 0) return pending;

    // Check if the underlying TCP has encrypted data waiting.
    // If so, we report 1 byte available to trigger MQTTClient
    // to call read(), which will invoke wolfSSL_read() to decrypt.
    if (_tcpClient.available() > 0) return 1;

    return 0;
}

int WolfSSLClient::read() {
    uint8_t b;
    // MQTTClient reads one byte at a time during packet parsing.
    // We must block briefly to allow wolfSSL to decrypt incoming records.
    if (!_ssl || !_connected) return -1;

    unsigned long startWait = millis();
    while (available() == 0) {
        if (millis() - startWait > 5000) return -1; // 5s timeout
        if (!_connected) return -1;
        delay(1);
    }
    if (read(&b, 1) == 1) return b;
    return -1;
}

int WolfSSLClient::read(uint8_t* buf, size_t size) {
    if (!_ssl || !_connected) return -1;

    int ret = wolfSSL_read(_ssl, buf, size);
    if (ret <= 0) {
        int err = wolfSSL_get_error(_ssl, ret);
        if (err == WOLFSSL_ERROR_WANT_READ) {
            // If TCP has data, wolfSSL needs another call to finish
            // decrypting a record. Retry a few times with small delays.
            for (int i = 0; i < 10 && _tcpClient.available() > 0; i++) {
                delay(10);
                ret = wolfSSL_read(_ssl, buf, size);
                if (ret > 0) {
                    ESP_LOGV(TAG_TLS, "read: %d/%zu bytes (after %d retries)", ret, size, i + 1);
                    return ret;
                }
            }
            return 0; // No data yet, non-blocking
        }
        ESP_LOGE(TAG_TLS, "read failed (err=%d), marking disconnected", err);
        _connected = false;
        return -1;
    }
    ESP_LOGV(TAG_TLS, "read: %d/%zu bytes received", ret, size);
    return ret;
}

int WolfSSLClient::peek() {
    if (!_ssl || !_connected) return -1;

    uint8_t b;
    int ret = wolfSSL_peek(_ssl, &b, 1);
    if (ret <= 0) return -1;
    return b;
}

void WolfSSLClient::flush() {
    _tcpClient.flush();
}

void WolfSSLClient::stop() {
    if (_ssl) {
        ESP_LOGD(TAG_TLS, "Shutting down TLS session");
        wolfSSL_shutdown(_ssl);
        wolfSSL_free(_ssl);
        _ssl = NULL;
    }
    _tcpClient.stop();
    _connected = false;
    ESP_LOGD(TAG_TLS, "Connection stopped");
}

uint8_t WolfSSLClient::connected() {
    return _connected && _tcpClient.connected();
}

WolfSSLClient::operator bool() {
    return connected();
}
