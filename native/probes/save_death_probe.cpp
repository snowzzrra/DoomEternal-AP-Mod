#include <windows.h>

#include "sentinel_inspection.h"
#include "protocol_v2.h"

#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <io.h>
#include <vector>

namespace {
using sentinel::generated::Bytes;

bool ReadExact(FILE* stream, uint8_t* bytes, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        const auto count = std::fread(bytes + offset, 1, size - offset, stream);
        if (!count) return false;
        offset += count;
    }
    return true;
}

bool WriteFrame(sentinel::generated::wire_header_2 header, const Bytes& body) {
    header.flags = 1;
    Bytes bytes;
    if (!sentinel::v2::frame(header, body, bytes)) return false;
    size_t offset = 0;
    while (offset < bytes.size()) {
        const auto count = std::fwrite(bytes.data() + offset, 1, bytes.size() - offset, stdout);
        if (!count) return false;
        offset += count;
    }
    return std::fflush(stdout) == 0;
}

bool PrepareReference(const Bytes& body, Bytes& result) {
    sentinel::generated::submission submission;
    sentinel::generated::command_reference reference;
    Bytes payload;
    return sentinel::v2::split_submission(body, submission, payload) &&
        sentinel::v2_reference(submission, payload, reference) &&
        sentinel::generated::encode(reference, result);
}

int RelayRequest(sentinel::V2Connection& connection, bool& connected, uint32_t pid,
                 sentinel::generated::wire_header_2 header, const Bytes& body) {
    Bytes result;
    if (header.operation == 0) {
        // operation zero computes a reference locally without submitting work
        if (!PrepareReference(body, result)) header.result = 4;
    } else {
        if (header.operation == 6 && !connected) {
            const auto status = connection.connect(pid, 1000);
            if (status != sentinel::ProbeResult::ok) return static_cast<int>(status);
            connected = true;
        }
        if (!connected || header.operation > 6) return 2;
        const auto response = connection.exchange(header.operation, body, 1000);
        if (response.transport != sentinel::ProbeResult::ok) return static_cast<int>(response.transport);
        header.result = static_cast<uint32_t>(response.wire);
        result = response.body;
    }
    return WriteFrame(header, result) ? 0 : 10;
}

int TransportRelay(const char* pidText) {
    char* end = nullptr;
    const auto pid = std::strtoul(pidText, &end, 10);
    if (!pid || !end || *end) return 2;
    if (_setmode(_fileno(stdin), _O_BINARY) < 0 || _setmode(_fileno(stdout), _O_BINARY) < 0) return 10;
    sentinel::V2Connection connection;
    bool connected = false;
    for (;;) {
        Bytes bytes(32);
        const int first = std::fgetc(stdin);
        if (first == EOF) return std::feof(stdin) ? 0 : 10;
        bytes[0] = static_cast<uint8_t>(first);
        if (!ReadExact(stdin, bytes.data() + 1, bytes.size() - 1)) return 9;
        sentinel::generated::wire_header_2 header{};
        if (!sentinel::v2::header(bytes, header, false)) return 9;
        bytes.resize(header.payload_length);
        if (!ReadExact(stdin, bytes.data(), bytes.size())) return 9;
        const int status = RelayRequest(connection, connected, pid, header, bytes);
        if (status) return status;
    }
}
}

using OodleLZDecompress = int WINAPI(
    uint8_t* src, int srcLen, uint8_t* dst, size_t dstSize,
    int fuzzSafe, int checkCrc, int verbosity,
    uint8_t* dstBase, size_t dstBaseSize,
    void* callback, void* callbackContext,
    void* scratch, size_t scratchSize, int threadPhase
);

static uint32_t ReadBigEndian32(const uint8_t* data) {
    return
        (static_cast<uint32_t>(data[0]) << 24) |
        (static_cast<uint32_t>(data[1]) << 16) |
        (static_cast<uint32_t>(data[2]) << 8) |
        static_cast<uint32_t>(data[3]);
}

static bool ReadFile(const char* path, std::vector<uint8_t>& output) {
    FILE* file = std::fopen(path, "rb");
    if (!file) {
        return false;
    }
    std::fseek(file, 0, SEEK_END);
    const long size = std::ftell(file);
    std::fseek(file, 0, SEEK_SET);
    if (size <= 0) {
        std::fclose(file);
        return false;
    }
    output.resize(static_cast<size_t>(size));
    const bool complete =
        std::fread(output.data(), 1, output.size(), file) == output.size();
    std::fclose(file);
    return complete;
}

static bool WriteFile(const char* path, const std::vector<uint8_t>& data) {
    FILE* file = std::fopen(path, "wb");
    if (!file) {
        return false;
    }
    const bool complete =
        std::fwrite(data.data(), 1, data.size(), file) == data.size();
    std::fclose(file);
    return complete;
}

int main(int argc, char** argv) {
    if (argc == 3 && std::strcmp(argv[1], "--transport-v2") == 0) {
        return TransportRelay(argv[2]);
    }
    if (argc != 3 && argc != 4) {
        std::fprintf(
            stderr,
            "usage: save_death_probe.exe <oo2core dll> <decrypted game_duration.dat> [decompressed output]\n"
        );
        return 2;
    }

    HMODULE library = LoadLibraryA(argv[1]);
    if (!library) {
        std::fprintf(stderr, "failed to load Oodle DLL\n");
        return 3;
    }
    auto* decompress = reinterpret_cast<OodleLZDecompress*>(
        GetProcAddress(library, "OodleLZ_Decompress")
    );
    if (!decompress) {
        std::fprintf(stderr, "OodleLZ_Decompress export not found\n");
        return 4;
    }

    std::vector<uint8_t> source;
    if (!ReadFile(argv[2], source)) {
        std::fprintf(stderr, "failed to read input\n");
        return 5;
    }
    if (source.size() < 28 || std::memcmp(source.data() + 8, "SlotFile", 8) != 0) {
        std::fprintf(stderr, "not a recognized SlotFile container\n");
        return 6;
    }

    const uint32_t tableOffset = ReadBigEndian32(source.data() + 20);
    if (tableOffset + 4 > source.size()) {
        std::fprintf(stderr, "invalid chunk table offset\n");
        return 7;
    }
    const uint32_t chunkCount = ReadBigEndian32(source.data() + tableOffset);
    if (chunkCount == 0 || tableOffset + 4ULL + chunkCount * 12ULL > source.size()) {
        std::fprintf(stderr, "invalid chunk count\n");
        return 8;
    }

    size_t inputOffset = 24;
    std::vector<uint8_t> unpacked;
    for (uint32_t index = 0; index < chunkCount; ++index) {
        const uint8_t* entry = source.data() + tableOffset + 4 + index * 12;
        const uint32_t unpackedSize = ReadBigEndian32(entry);
        const uint32_t packedSize = ReadBigEndian32(entry + 4);
        if (
            unpackedSize == 0 || unpackedSize > 131072 ||
            inputOffset + packedSize > tableOffset
        ) {
            std::fprintf(stderr, "invalid chunk %u\n", index);
            return 9;
        }

        const size_t outputOffset = unpacked.size();
        unpacked.resize(outputOffset + unpackedSize + 64);
        const int actual = decompress(
            source.data() + inputOffset,
            static_cast<int>(packedSize),
            unpacked.data() + outputOffset,
            unpackedSize,
            0, 0, 0, nullptr, 0, nullptr, nullptr, nullptr, 0, 0
        );
        if (actual != static_cast<int>(unpackedSize)) {
            std::fprintf(stderr, "failed to decompress chunk %u\n", index);
            return 10;
        }
        unpacked.resize(outputOffset + unpackedSize);
        inputOffset += packedSize;
    }

    if (argc == 4 && !WriteFile(argv[3], unpacked)) {
        std::fprintf(stderr, "failed to write decompressed output\n");
        return 12;
    }

    static constexpr char marker[] = "numCheckpointDeaths";
    for (size_t offset = 0; offset + sizeof(marker) + 1 < unpacked.size(); ++offset) {
        if (std::memcmp(unpacked.data() + offset, marker, sizeof(marker) - 1) != 0) {
            continue;
        }
        const size_t typeOffset = offset + sizeof(marker) - 1;
        if (unpacked[typeOffset] != 0x01) {
            continue;
        }
        const uint8_t deaths = unpacked[typeOffset + 1];
        std::printf("numCheckpointDeaths=%u\n", deaths);
        return deaths > 0 ? 20 : 0;
    }

    std::fprintf(stderr, "numCheckpointDeaths not found\n");
    return 11;
}
