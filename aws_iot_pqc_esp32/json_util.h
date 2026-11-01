// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// json_util.h — minimal whitespace-tolerant JSON value extractors.
// These handle both compact ({"k":"v"}) and spaced ({"k": "v"}) JSON, since
// Python's json.dumps() (used by the OTA Lambda) inserts a space after the colon.

#ifndef JSON_UTIL_H
#define JSON_UTIL_H

#include <Arduino.h>

// Returns the string value for key, or "" if not found.
String jsonGetString(const String& json, const char* key);
// Returns the numeric value for key, or 0 if not found.
long jsonGetNumber(const String& json, const char* key);

#endif  // JSON_UTIL_H
