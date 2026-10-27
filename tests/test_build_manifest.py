import pytest

from doom_eap.runtime.save_decrypt import _aes_gcm_decrypt, _aes_gcm_encrypt


def test_aes_gcm_matches_nist_vector_and_rejects_corrupted_data():
    encrypted = bytes.fromhex('0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf')
    assert _aes_gcm_encrypt(bytes(16), bytes(12), bytes(16), b'') == encrypted
    assert _aes_gcm_decrypt(bytes(16), bytes(12), encrypted[:-16], encrypted[-16:], b'') == bytes(16)
    with pytest.raises(ValueError, match='authentication'):
        _aes_gcm_decrypt(bytes(16), bytes(12), encrypted[:-16], bytes(16), b'')
