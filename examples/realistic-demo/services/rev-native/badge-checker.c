#include <stdint.h>
#include <stdio.h>
#include <string.h>

static const char *legacy_tokens[] = {
    "rhombus{native_test_token_v1}",
    "flag{strings_output_is_not_validation}",
};
static const uint8_t key[] = {0x72, 0x69, 0x64, 0x67, 0x65};
static const uint8_t expected[] = {
    0x00, 0x10, 0x68, 0x68, 0x58, 0x60, 0x01, 0x31, 0x88, 0x68, 0x80, 0x58,
    0xf0, 0x78, 0x42, 0x01, 0xb0, 0x88, 0xa0, 0x6a, 0xa8, 0x91, 0x19, 0x11,
    0x09, 0x32, 0xd8, 0x01, 0x81, 0xba, 0xf0, 0xd9, 0x41, 0x19, 0xc1, 0x91,
};

static uint8_t rol8(uint8_t value, unsigned amount) {
    return (uint8_t)((value << amount) | (value >> (8 - amount)));
}

static int valid(const char *candidate) {
    size_t length = strlen(candidate);
    if (length != sizeof(expected)) {
        return 0;
    }
    for (size_t i = 0; i < length; i++) {
        uint8_t mixed = (uint8_t)(((uint8_t)candidate[i] ^ key[i % sizeof(key)]) + i);
        if (rol8(mixed, 3) != expected[i]) {
            return 0;
        }
    }
    return 1;
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "--legacy-fixture") == 0) {
        puts(legacy_tokens[0]);
        return 2;
    }
    if (argc != 2) {
        fprintf(stderr, "usage: %s <maintenance-code>\n", argv[0]);
        return 2;
    }
    puts(valid(argv[1]) ? "maintenance code accepted" : "invalid maintenance code");
    return valid(argv[1]) ? 0 : 1;
}
