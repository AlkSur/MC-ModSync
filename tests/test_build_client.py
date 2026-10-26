"""The generated fallback must be reproducible across Git checkout platforms."""
from tools import build_client


def test_payload_normalizes_windows_source_newlines(tmp_path):
    source = tmp_path / "example.py"
    modules = [("mcmodsync.example", str(source))]
    source.write_bytes(b"value = 1\nprint(value)\n")
    linux_payload = build_client.build_zip(modules)
    source.write_bytes(b"value = 1\r\nprint(value)\r\n")
    assert build_client.build_zip(modules) == linux_payload
