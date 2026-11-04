// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "json_util.h"


// ─── Minimal whitespace-tolerant JSON value extractors ─────────────────────
// These handle both compact ({"k":"v"}) and spaced ({"k": "v"}) JSON, since
// Python's json.dumps() (used by the OTA Lambda) inserts a space after the colon.

// Returns the string value for key, or "" if not found.
String jsonGetString(const String& json, const char* key) {
    String needle = String("\"") + key + "\"";
    int k = json.indexOf(needle);
    if (k < 0) return String("");
    int colon = json.indexOf(':', k + needle.length());
    if (colon < 0) return String("");
    int i = colon + 1;
    while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) i++;
    if (i >= (int)json.length() || json[i] != '"') return String("");
    int start = i + 1;
    int end = json.indexOf('"', start);
    if (end < 0) return String("");
    return json.substring(start, end);
}

// Returns the numeric value for key, or 0 if not found.
long jsonGetNumber(const String& json, const char* key) {
    String needle = String("\"") + key + "\"";
    int k = json.indexOf(needle);
    if (k < 0) return 0;
    int colon = json.indexOf(':', k + needle.length());
    if (colon < 0) return 0;
    int i = colon + 1;
    while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) i++;
    return json.substring(i).toInt();
}
