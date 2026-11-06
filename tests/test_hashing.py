"""Exact byte provenance and bounded file hashing on Python 3.10 and later."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ingestion_support import sha256_file


class FileHashingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'evidence.bin'

    def test_standard_empty_and_ascii_vectors(self):
        vectors = [
            (b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
            (b'abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'),
        ]
        for payload, expected in vectors:
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                self.assertEqual(sha256_file(self.path), expected)

    def test_binary_bytes_across_chunk_boundaries(self):
        payload = bytes(range(256)) * 8192 + b'\x00\xff\r\n\x80'
        self.path.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(sha256_file(str(self.path)), expected)
        # Altering one byte beyond the first chunk must change the digest.
        changed = bytearray(payload)
        changed[1024 * 1024 + 17] ^= 0xff
        self.path.write_bytes(changed)
        self.assertNotEqual(sha256_file(self.path), expected)
        self.assertEqual(sha256_file(self.path), hashlib.sha256(changed).hexdigest())

    def test_streaming_reads_remain_bounded_and_accept_short_reads(self):
        payload = bytes(range(256)) * 8192 + b'last-part'
        requests = []

        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                requests.append(size)
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError('hashing must read bounded binary chunks')
                return super().read(min(size, 997))

        reader = ShortReader(payload)
        with mock.patch('ingestion_support.open', return_value=reader):
            self.assertEqual(sha256_file(self.path), hashlib.sha256(payload).hexdigest())
        self.assertGreater(len(requests), 2)
        self.assertTrue(reader.closed)

    def test_does_not_require_python_311_file_digest(self):
        self.path.write_bytes(b'\x00\xffsource-bytes')
        with mock.patch.object(hashlib, 'file_digest', create=True,
                               side_effect=AssertionError('unavailable on Python 3.10')):
            self.assertEqual(sha256_file(self.path),
                             hashlib.sha256(b'\x00\xffsource-bytes').hexdigest())


if __name__ == '__main__':
    unittest.main()
