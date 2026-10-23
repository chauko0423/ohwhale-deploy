"""Real loopback TLS verifies that adding CA roots never disables validation."""
import datetime
import hashlib
import http.server
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ohwhale.download import destination, fetch


class TLSDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ohwhale-tls-')
        cls.root = Path(cls.temp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        cls.ca = cls.root / 'test-ca.pem'
        cls.ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path = cls.root / 'test-key.pem'
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        cls.payload = b'pinned HTTPS download fixture'

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(cls.payload)))
                self.end_headers()
                self.wfile.write(cls.payload)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cls.ca, key_path)
        cls.server.socket = server_context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def entry(self, name, host='localhost'):
        return {'id': name, 'destination': name + '.bin', 'size': len(self.payload),
                'sha256': hashlib.sha256(self.payload).hexdigest(),
                'urls': [f'https://{host}:{self.server.server_port}/fixture']}

    def test_bundle_supplies_missing_root(self):
        # The generated CA is absent from system roots; only the added bundle
        # can make this request succeed.
        entry = self.entry('trusted')
        with patch('certifi.where', return_value=str(self.ca)):
            self.assertEqual(fetch(entry, self.root, attempts=1), 'downloaded and verified')
        self.assertEqual(destination(entry, self.root).read_bytes(), self.payload)

    def test_untrusted_certificate_is_rejected(self):
        entry = self.entry('untrusted')
        with self.assertRaisesRegex(RuntimeError, 'CERTIFICATE_VERIFY_FAILED'):
            fetch(entry, self.root, attempts=1)
        self.assertFalse(destination(entry, self.root).exists())

    def test_hostname_mismatch_is_rejected(self):
        entry = self.entry('wrong-host', '127.0.0.1')
        with patch('certifi.where', return_value=str(self.ca)):
            with self.assertRaisesRegex(RuntimeError, 'CERTIFICATE_VERIFY_FAILED'):
                fetch(entry, self.root, attempts=1)
        self.assertFalse(destination(entry, self.root).exists())


if __name__ == '__main__':
    unittest.main()
