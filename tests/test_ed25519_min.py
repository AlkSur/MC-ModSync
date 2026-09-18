"""TL(T-11): ed25519_min.py —— RFC 8032 §7.1 官方测试向量 + 跨实现互验。

向量来源: RFC 8032 Section 7.1（逐字转录，未改写）。
跨实现互验: cryptography 生成签名 -> ed25519_min 验签通过。
"""
from __future__ import annotations

import os

import pytest

from mcmodsync import ed25519_min

# ---- RFC 8032 §7.1 官方向量 (公钥, 消息, 签名) ----
M1 = ""
M2 = "72"
M3 = "af82"
M1024 = (
    "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98"
    "fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8"
    "79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
    "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc"
    "1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe"
    "ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
    "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef"
    "efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7"
    "aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
    "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2"
    "d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
    "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
    "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
    "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07"
    "07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
    "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a"
    "ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e"
    "c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
    "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c"
    "42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8"
    "ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
    "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
    "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649"
    "de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
    "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3"
    "2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
    "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
    "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5"
    "0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1"
    "369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
    "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c"
    "0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"
)
MSHA = (
    "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
    "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
)

VECTORS = [
    ("TEST 1", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     M1, "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("TEST 2", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     M2, "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("TEST 3", "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     M3, "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("TEST 1024", "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
     M1024, "0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03"),
    ("TEST SHA(abc)", "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     MSHA, "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


@pytest.mark.parametrize("name,pub,msg,sig", VECTORS, ids=[v[0] for v in VECTORS])
def test_official_vector_verifies(name: str, pub: str, msg: str, sig: str) -> None:
    assert ed25519_min.verify(_b(pub), _b(sig), _b(msg)) is True, name


@pytest.mark.parametrize("name,pub,msg,sig", VECTORS, ids=[v[0] for v in VECTORS])
def test_official_vector_matches_cryptography(name: str, pub: str, msg: str, sig: str) -> None:
    """向量完整性交叉校验（cryptography 作为对照实现）。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    try:
        Ed25519PublicKey.from_public_bytes(_b(pub)).verify(_b(sig), _b(msg))
    except InvalidSignature:  # pragma: no cover
        pytest.fail("%s 向量与 cryptography 不一致（转录有误）" % name)


def test_test1024_message_is_1023_bytes() -> None:
    assert len(_b(M1024)) == 1023


def test_tampered_signature_fails() -> None:
    name, pub, msg, sig = VECTORS[2]
    raw = bytearray(_b(sig))
    raw[0] ^= 0x01
    assert ed25519_min.verify(_b(pub), bytes(raw), _b(msg)) is False
    for idx in (0, 31, 32, 63):
        raw = bytearray(_b(sig))
        raw[idx] ^= 0x01
        assert ed25519_min.verify(_b(pub), bytes(raw), _b(msg)) is False


def test_tampered_message_fails() -> None:
    name, pub, msg, sig = VECTORS[2]
    tampered = bytes([_b(msg)[0] ^ 0x01]) + _b(msg)[1:]
    assert ed25519_min.verify(_b(pub), _b(sig), tampered) is False


def test_tampered_public_key_fails() -> None:
    name, pub, msg, sig = VECTORS[1]
    bad_pub = bytearray(_b(pub))
    bad_pub[0] ^= 0x01
    assert ed25519_min.verify(bytes(bad_pub), _b(sig), _b(msg)) is False


@pytest.mark.parametrize("pub_len,sig_len", [(31, 64), (33, 64), (32, 63), (32, 65), (0, 0)])
def test_malformed_lengths_rejected(pub_len: int, sig_len: int) -> None:
    assert ed25519_min.verify(b"\x00" * pub_len, b"\x00" * sig_len, b"m") is False


def test_non_bytes_rejected() -> None:
    assert ed25519_min.verify("x", b"\x00" * 64, b"m") is False  # type: ignore[arg-type]


def test_s_ge_l_rejected() -> None:
    name, pub, msg, sig = VECTORS[1]
    raw = bytearray(_b(sig))
    raw[32:] = (2 ** 252 + 27742317777372353535851937790883648493).to_bytes(32, "little")
    assert ed25519_min.verify(_b(pub), bytes(raw), _b(msg)) is False


@pytest.mark.parametrize("size", [0, 1, 2, 63, 64, 65, 255, 1023, 1024, 4096])
def test_cross_implementation_sign_then_verify(size: int) -> None:
    """cryptography 签名 -> ed25519_min 验签（覆盖各长度，含 1024）。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    msg = os.urandom(size)
    sig = key.sign(msg)
    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    assert ed25519_min.verify(pub_raw, sig, msg) is True
    assert ed25519_min.verify(pub_raw, sig, msg + b"\x00") is False
