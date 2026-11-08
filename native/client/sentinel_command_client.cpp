#include "sentinel_command_client.h"
#include "commands.h"
#include <algorithm>
#include <bcrypt.h>
#include <cstring>
#include <sstream>

namespace g = sentinel::generated;
const char* RpcCallResultName(ApRpcResult result) {
    static const char* names[] = {"NONE", "PIPE_NOT_FOUND", "PIPE_BUSY", "TIMEOUT", "DISPATCHED",
        "EXCEPTION", "UNKNOWN", "EFFECT_UNKNOWN", "REFUSED"};
    return result >= AP_RPC_NONE && result <= AP_RPC_REJECTED ? names[result] : "UNKNOWN";
}
void SentinelCommandClient::SetTargetProcess(DWORD pid) {
    if (pid_ != pid) { connection_.close(); pid_ = pid; ready_ = false; identity_ = {}; }
}
template<class T> bool SentinelCommandClient::Read(uint16_t type, const g::Bytes& payload, T& value) {
    g::read_envelope input{};
    input.scope = scope_;
    input.namespace_id.fill('0');
    input.command_type = type;
    input.payload_revision = 1;
    g::Bytes body;
    if (!g::encode(input, body)) return false;
    body.insert(body.end(), payload.begin(), payload.end());
    const auto reply = connection_.exchange(1, body, 500);
    status_ = reply.win32_error;
    if (reply.transport != sentinel::ProbeResult::ok || reply.wire != sentinel::WireResult::ok || reply.body.size() < 36) return false;
    g::scope actual{};
    if (!g::decode(g::Bytes(reply.body.begin(), reply.body.begin() + 36), actual)
            || actual.pid != scope_.pid || actual.process_created != scope_.process_created || actual.instance_id != scope_.instance_id) return false;
    return g::decode(g::Bytes(reply.body.begin() + 36, reply.body.end()), value);
}
bool SentinelCommandClient::PollHealth() {
    if (!pid_) { ready_ = false; result_ = AP_RPC_PIPE_MISSING; return false; }
    if (connection_.identity().pid != pid_) {
        if (connection_.connect(pid_, 500) != sentinel::ProbeResult::ok) {
            connection_.close(); ready_ = false; result_ = AP_RPC_PIPE_MISSING; return false;
        }
    }
    const auto& native = connection_.identity();
    scope_.pid = native.pid; scope_.process_created = native.process_created;
    std::copy_n(native.instance_id, 16, scope_.instance_id.begin());
    scope_.generation = native.lifecycle_generation;
    g::Bytes payload;
    g::encode(g::native_payload{g::native_native_payload{}}, payload);
    g::sc_native_snapshot snapshot{};
    ready_ = Read(4, payload, snapshot) && snapshot.availability == SC_NATIVE_ENABLED;
    if (!ready_) { connection_.close(); result_ = AP_RPC_UNKNOWN; return false; }
    scope_.generation = snapshot.scope.lifecycle_generation;
    if (identity_.process_created != scope_.process_created || identity_.instance != scope_.instance_id) ++epoch_;
    identity_.pid = pid_; identity_.process_created = scope_.process_created; identity_.instance = scope_.instance_id;
    result_ = AP_RPC_DELIVERED;
    return true;
}
sentinel::Inspection SentinelCommandClient::SaveAdmission() {
    sentinel::Inspection out{};
    g::Bytes payload;
    g::encode(g::save_admission_payload{g::save_admission_save_admission_payload{}}, payload);
    g::save_admission_result value{};
    if (!PollHealth() || !Read(12, payload, value)) return out;
    out.result = sentinel::ProbeResult::ok;
    out.snapshot = identity_;
    out.admission.state = value.observation.state;
    out.admission.flags = value.observation.flags;
    std::copy(value.observation.namespace_id.begin(), value.observation.namespace_id.end(), out.admission.namespace_id);
    std::copy(value.observation.native_root.begin(), value.observation.native_root.end(), out.admission.native_root);
    binding_ = value.save_binding;
    return out;
}
static bool BuildCommandPayload(const std::string& text, const sc_command_request& checked, g::command_payload& payload) {
    std::istringstream words(text);
    std::string first, second, third;
    words >> first >> second >> third;
    if (checked.kind == SC_COMMAND_ACTIVATE) payload = g::command_activate_payload{second};
    else if (checked.kind == SC_COMMAND_ECHO) payload = g::command_echo_payload{text.substr(5)};
    else if (checked.kind == SC_COMMAND_CONDUMP) payload = g::command_condump_payload{second};
    else if (checked.kind == SC_COMMAND_TRANSIENT_POLICY) {
        const uint32_t variable = first == "g_damageScaleAllToAI" ? 0 : first == "g_damageScaleAllToSlayer" ? 1 : 2;
        payload = g::command_transient_policy_payload{variable, second};
    } else { return false; }
    return true;
}

static bool CreateCommandId(g::submission& input) {
    std::array<uint8_t, 16> random{};
    if (BCryptGenRandom(nullptr, random.data(), 16, BCRYPT_USE_SYSTEM_PREFERRED_RNG)) return false;
    std::ostringstream id;
    for (const auto byte : random) id << std::hex << static_cast<unsigned>(byte) << '-';
    input.reference.command_id = id.str();
    return true;
}

static bool EncodeCommandSubmission(const g::submission& input, const g::command_payload& payload, g::command_reference& ref, g::Bytes& body, g::Bytes& reference) {
    g::Bytes encoded;
    if (!g::encode(payload, encoded) || !sentinel::v2_reference(input, encoded, ref)
            || !g::encode(ref, reference) || !g::encode(input, body)) return false;
    body.insert(body.end(), encoded.begin(), encoded.end());
    return true;
}

bool SentinelCommandClient::ExecuteConsoleCommand(const std::string& text) {
    sc_command_request checked{};
    if (!sentinel::commands::parse(text, checked) || !PollHealth()) { result_ = AP_RPC_REJECTED; return false; }
    const auto admission = SaveAdmission();
    if (admission.result != sentinel::ProbeResult::ok || !(admission.admission.flags & SC_SAVE_SESSION_ACCEPTING)) return false;
    g::command_payload payload;
    if (!BuildCommandPayload(text, checked, payload)) { result_ = AP_RPC_REJECTED; return false; }
    g::submission input{};
    input.reference.scope = scope_;
    std::copy_n(admission.admission.namespace_id, 64, input.reference.namespace_id.begin());
    if (!CreateCommandId(input)) return false;
    input.reference.command_type = 46; input.reference.payload_revision = 1;
    input.save_binding = binding_; input.deadline_ms = 2000;
    g::Bytes body, reference;
    g::command_reference ref{};
    if (!EncodeCommandSubmission(input, payload, ref, body, reference)) return false;
    return AwaitCommand(body, reference, ref);
}

static bool ReadCommandReply(const sentinel::V2Response& response, g::command_reply& reply, const g::Bytes& reference, const std::array<uint8_t, 16>& instance, g::Bytes& result) {
        sentinel::v2::CommandError error{};
        g::sc_diagnostic_result execution{};
        if (response.transport != sentinel::ProbeResult::ok || response.wire != sentinel::WireResult::ok
                || !sentinel::v2::command_response(response.body, reply, error, execution, result)
                || error != sentinel::v2::CommandError::none || reply.actual_instance_id != instance) return false;
        g::Bytes returnedReference;
        if (!g::encode(reply.reference, returnedReference) || returnedReference != reference) return false;
        return true;
}

bool SentinelCommandClient::AwaitCommand(const g::Bytes& body, const g::Bytes& reference, const g::command_reference& ref) {
    auto response = connection_.exchange(2, body, 500);
    const auto deadline = GetTickCount64() + 4000;
    for (;;) {
        g::command_reply reply{};
        g::Bytes result;
        if (!ReadCommandReply(response, reply, reference, scope_.instance_id, result)) break;
        if (reply.dimensions.progress == 2) {
            g::sc_command_result value{};
            if (!g::decode(result, value)) break;
            g::Bytes ack;
            g::encode(g::ack{ref, reply.dimensions.result_token}, ack);
            connection_.exchange(5, ack, 500);
            result_ = value.outcome == SC_COMMAND_DISPATCHED ? AP_RPC_DELIVERED : AP_RPC_REJECTED;
            status_ = value.native_exception;
            return result_ == AP_RPC_DELIVERED;
        }
        if (GetTickCount64() >= deadline) break;
        Sleep(10);
        response = connection_.exchange(3, reference, 500);
    }
    result_ = AP_RPC_AMBIGUOUS; status_ = ERROR_TIMEOUT;
    return false;
}
