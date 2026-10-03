#include "../client/ammo_hotkey.h"
#include <cassert>
#include <cstdio>
#include <fstream>

int main(int argc, char** argv) {
    assert(argc == 2);
    const auto check = [&](const std::string& data, int expected) {
        { std::ofstream file(argv[1], std::ios::binary); file.write(data.data(), data.size()); }
        const AmmoHotkeyHandler handler(nullptr, argv[1]);
        assert(handler.GetVirtualKey() == expected);
    };
    check("AP_AMMO_REFILL_HOTKEY_V1\nY\n", 'Y');
    check("AP_AMMO_REFILL_HOTKEY_V1 B\n", 'B');
    check("Y\n", 'Y');
    check("AP_AMMO_REFILL_HOTKEY_V1 UNBOUND\n", 0);
    check("AP_AMMO_REFILL_HOTKEY_V1 Y extra\n", 0);
    check("Y B\n", 0);
    check("AP_SPECIAL_TOGGLE_HOTKEY_V1 Y\n", 0);
    check(std::string("Y\0", 2), 0);
    check(std::string(128, 'Y'), 0);
    check("F999999999999999999\n", 0);
    check("PAGE_UP\n", 0);
    check("F00001\n", VK_F1);
    assert(AmmoHotkeyHandler::TokenToVirtualKey("F12") == VK_F12);
    std::remove(argv[1]);
    std::puts("PASS ammo hotkey whitespace codec / UNBOUND / malformed input");
}
