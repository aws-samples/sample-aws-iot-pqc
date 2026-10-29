// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Kept in a header so the Arduino preprocessor's auto-generated function
// prototypes (inserted near the top of the .ino) can see the type.
#pragma once
#include <stddef.h>
#include <stdint.h>

struct Blob {
    uint8_t* data = nullptr;
    size_t   len  = 0;
};
