from pathlib import Path


NATIVE_CLIENT_SOURCE = (
    Path(__file__).parents[1] / "native" / "client" / "ap_client_exe.cpp"
)
PLAYABLE_BUILD_SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "build" / "playable_test.sh"
)


def test_native_identity_namespaces_are_explicit_and_current():
    source = NATIVE_CLIENT_SOURCE.read_text(encoding="utf-8")

    assert 'kReleaseVersion = "0.4.0-beta.2"' in source
    assert 'kRpcEntityPrefix = "ap_rpc_v3"' in source
    assert "kRpcEntityContractRevision = 3" in source
    assert "kNativeCommandPolicyRevision = 7" in source
    assert "bridge_protocol_version=unavailable" in source
    assert "rpc_entity_contract_revision=" in source
    assert "rpc_entity_prefix=" in source
    assert "NATIVE_COMMAND_POLICY_REVISION:" in source

    assert "v0.3.9-alpha" not in source
    assert "ITEM_MAPPING_REVISION" not in source
    assert "protocol_version=3" not in source


def test_native_identity_gate_is_pipefail_safe():
    script = PLAYABLE_BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'strings "$CLIENT_BUILD_DIR/ap_client.exe" > "$CLIENT_STRINGS_FILE"' in script
    assert 'strings "$CLIENT_BUILD_DIR/ap_client.exe" | grep' not in script
