#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <vector>

typedef int WINAPI OodleLZDecompress(
    uint8_t* src, int srcLen, uint8_t* dst, size_t dstSize,
    int fuzzSafe, int checkCrc, int verbosity,
    uint8_t* dstBase, size_t e, void* callback, void* callbackContext,
    void* scratch, size_t scratchSize, int threadPhase
);

uint32_t ReadBigEndian32(const uint8_t* data) {
    return
        (static_cast<uint32_t>(data[0]) << 24) |
        (static_cast<uint32_t>(data[1]) << 16) |
        (static_cast<uint32_t>(data[2]) << 8) |
        static_cast<uint32_t>(data[3]);
}

int main(int argc, char** argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: save_oodle_probe.exe <oo2core dll> <input> <output>\n");
        return 2;
    }

    HMODULE library = LoadLibraryA(argv[1]);
    if (!library) {
        fprintf(stderr, "failed to load Oodle DLL: %lu\n", GetLastError());
        return 3;
    }
    OodleLZDecompress* decompress = reinterpret_cast<OodleLZDecompress*>(
        GetProcAddress(library, "OodleLZ_Decompress")
    );
    if (!decompress) {
        fprintf(stderr, "OodleLZ_Decompress export not found\n");
        return 4;
    }

    FILE* input = fopen(argv[2], "rb");
    if (!input) return 5;
    fseek(input, 0, SEEK_END);
    const long inputSize = ftell(input);
    fseek(input, 0, SEEK_SET);
    std::vector<uint8_t> source(inputSize);
    fread(source.data(), 1, source.size(), input);
    fclose(input);

    if (source.size() < 24 || memcmp(source.data() + 8, "SlotFile", 8) != 0) {
        fprintf(stderr, "not a recognized SlotFile container\n");
        return 6;
    }

    const uint32_t compressedSize = ReadBigEndian32(source.data() + 16);
    const uint32_t outputSize = ReadBigEndian32(source.data() + 20);
    if (24ULL + compressedSize > source.size()) {
        fprintf(stderr, "invalid first block sizes: compressed=%u output=%u file=%zu\n",
            compressedSize, outputSize, source.size());
        return 7;
    }

    std::vector<uint8_t> output(outputSize);
    int actual = decompress(
        source.data() + 24, compressedSize, output.data(), output.size(),
        1, 1, 0, nullptr, 0, nullptr, nullptr, nullptr, 0, 0
    );
    if (actual <= 0) {
        const size_t remaining = source.size() - 24;
        actual = decompress(
            source.data() + 24, static_cast<int>(remaining), output.data(), output.size(),
            1, 1, 0, nullptr, 0, nullptr, nullptr, nullptr, 0, 0
        );
    }
    FILE* diagnostics = fopen("save_oodle_probe.log", "a");
    if (diagnostics) {
        fprintf(diagnostics, "compressed=%u remaining=%zu expected=%u actual=%d\n",
            compressedSize, source.size() - 24, outputSize, actual);
        fclose(diagnostics);
    }
    if (actual <= 0) return 8;

    FILE* result = fopen(argv[3], "wb");
    if (!result) return 9;
    fwrite(output.data(), 1, actual, result);
    fclose(result);
    return 0;
}
