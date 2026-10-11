import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid

from prototypes.ssh_delivery.client import send
from prototypes.ssh_delivery.receiver import MAX_PAYLOAD_BYTES, Rejected, receive


TRANSACTION = "11111111-1111-4111-8111-111111111111"
CREDENTIALS = {"access_token": "FAKE_ACCESS_TOKEN", "refresh_token": "FAKE_REFRESH_TOKEN", "client_id": "FAKE_CLIENT_ID"}


def payload(transaction=TRANSACTION, credentials=None, expires_at=400):
    return json.dumps({"version": 1, "transaction_id": transaction, "expires_at": expires_at,
                       "credentials": CREDENTIALS if credentials is None else credentials}).encode()


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def test_accepts_once_and_acknowledges_without_credentials(self):
        result = receive(io.BytesIO(payload()), self.directory.name, TRANSACTION, now=100)
        self.assertEqual(result, {"status": "received", "transaction_id": TRANSACTION})
        self.assertTrue(Path(self.directory.name, hashlib.sha256(TRANSACTION.encode()).hexdigest()).exists())

    def test_rejects_expired_invalid_and_unauthorized_transaction(self):
        for body, transaction in ((payload(expires_at=100), TRANSACTION), (b"not json", TRANSACTION),
                                  (payload(transaction=str(uuid.uuid4())), TRANSACTION)):
            with self.subTest(transaction=transaction), self.assertRaises(Rejected):
                receive(io.BytesIO(body), self.directory.name, transaction, now=100)

    def test_rejects_oversize_and_replay(self):
        with self.assertRaises(Rejected):
            receive(io.BytesIO(b"x" * (MAX_PAYLOAD_BYTES + 1)), self.directory.name, TRANSACTION, now=100)
        receive(io.BytesIO(payload()), self.directory.name, TRANSACTION, now=100)
        with self.assertRaises(Rejected):
            receive(io.BytesIO(payload()), self.directory.name, TRANSACTION, now=100)

    def test_rejects_unknown_credential_fields_and_duplicate_json_keys(self):
        credentials = CREDENTIALS | {"unexpected": "value"}
        with self.assertRaises(Rejected):
            receive(io.BytesIO(payload(credentials=credentials)), self.directory.name, TRANSACTION, now=100)
        with self.assertRaises(Rejected):
            receive(io.BytesIO(payload().replace(b'"version": 1', b'"version": true')),
                    self.directory.name, TRANSACTION, now=100)
        with self.assertRaises(Rejected):
            receive(io.BytesIO(b'{"version":1,"version":1}'), self.directory.name, TRANSACTION, now=100)


@unittest.skipUnless(os.environ.get("NEXO_RUN_SSH_POC") == "1" and shutil.which("docker"),
                     "set NEXO_RUN_SSH_POC=1 to run isolated Docker/OpenSSH integration")
class OpenSSHIntegrationTests(unittest.TestCase):
    def test_strict_host_auth_restricted_stdin_receiver_and_safe_ack(self):
        tag = f"nexo-ssh-poc:{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="nexo-ssh-poc-") as temp:
            temp_path = Path(temp)
            identity = temp_path / "client_key"
            known_hosts = temp_path / "known_hosts"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)], check=True)
            build = subprocess.run(["docker", "build", "-t", tag, "-f", "prototypes/ssh_delivery/Dockerfile",
                                    "prototypes/ssh_delivery"], capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr)
            container = subprocess.run(["docker", "run", "-d", "--rm", "-p", "127.0.0.1::2222", tag],
                                       capture_output=True, text=True, check=True).stdout.strip()
            try:
                subprocess.run(["docker", "exec", "-i", container, "sh", "-c",
                                "cat > /etc/ssh/authorized_keys/nexo-import && chmod 644 /etc/ssh/authorized_keys/nexo-import"],
                               input=b"restrict " + identity.with_suffix(".pub").read_bytes(), check=True)
                subprocess.run(["docker", "exec", "-i", container, "sh", "-c",
                                "cat > /run/allowed-transaction && chmod 444 /run/allowed-transaction"],
                               input=TRANSACTION.encode(), check=True)
                port_text = subprocess.run(["docker", "port", container, "2222/tcp"], capture_output=True,
                                           text=True, check=True).stdout.strip().rsplit(":", 1)[1]
                port = int(port_text)
                host_key = ""
                for _ in range(60):
                    result = subprocess.run(["docker", "exec", container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
                                            capture_output=True, text=True)
                    if result.returncode == 0 and result.stdout:
                        host_key = result.stdout.strip()
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                break
                        except OSError:
                            pass
                    time.sleep(0.1)
                self.assertTrue(host_key)
                known_hosts.write_text(f"[127.0.0.1]:{port} {host_key}\n")
                known_hosts.chmod(0o600)

                request = payload(expires_at=int(time.time()) + 120)
                delivered = send("127.0.0.1", port, str(identity), str(known_hosts), request)
                logs = subprocess.run(["docker", "logs", container], capture_output=True).stderr
                self.assertEqual(delivered.returncode, 0, delivered.stdout.decode() + delivered.stderr.decode() + logs.decode())
                self.assertEqual(json.loads(delivered.stdout)["status"], "received")
                self.assertNotIn(b"FAKE_", delivered.stdout + delivered.stderr)

                replay = send("127.0.0.1", port, str(identity), str(known_hosts), request)
                self.assertNotEqual(replay.returncode, 0)
                self.assertNotIn(b"FAKE_", replay.stdout + replay.stderr)

                wrong_transaction = send("127.0.0.1", port, str(identity), str(known_hosts),
                                          payload(transaction=str(uuid.uuid4()), expires_at=int(time.time()) + 120))
                self.assertNotEqual(wrong_transaction.returncode, 0)
                self.assertNotIn(b"FAKE_", wrong_transaction.stdout + wrong_transaction.stderr)

                with self.assertRaises(ValueError):
                    send("localhost", port, str(identity), str(known_hosts), request)
                wrong_host = temp_path / "wrong_host_key"
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_host)], check=True)
                known_hosts.write_text(f"[127.0.0.1]:{port} {wrong_host.with_suffix('.pub').read_text().split(' ', 2)[0]} "
                                       f"{wrong_host.with_suffix('.pub').read_text().split(' ', 2)[1]}\n")
                wrong_destination = send("127.0.0.1", port, str(identity), str(known_hosts), request)
                self.assertNotEqual(wrong_destination.returncode, 0)
                self.assertNotIn(b"FAKE_", wrong_destination.stdout + wrong_destination.stderr)

                unauthorized_key = temp_path / "unauthorized_key"
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unauthorized_key)], check=True)
                known_hosts.write_text(f"[127.0.0.1]:{port} {host_key}\n")
                unauthorized = send("127.0.0.1", port, str(unauthorized_key), str(known_hosts), request)
                self.assertNotEqual(unauthorized.returncode, 0)
                self.assertNotIn(b"FAKE_", unauthorized.stdout + unauthorized.stderr)

                invalid = send("127.0.0.1", port, str(identity), str(known_hosts), b"not-json")
                self.assertNotEqual(invalid.returncode, 0)
                oversized = send("127.0.0.1", port, str(identity), str(known_hosts), b"x" * (MAX_PAYLOAD_BYTES + 1))
                self.assertNotEqual(oversized.returncode, 0)

                for path in temp_path.iterdir():
                    if path.is_file():
                        content = path.read_bytes()
                        self.assertNotIn(b"FAKE_ACCESS_TOKEN", content)
                        self.assertNotIn(b"FAKE_REFRESH_TOKEN", content)
                log_result = subprocess.run(["docker", "logs", container], capture_output=True)
                logs = log_result.stdout + log_result.stderr
                self.assertNotIn(b"FAKE_ACCESS_TOKEN", logs)
                self.assertNotIn(b"FAKE_REFRESH_TOKEN", logs)
            finally:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
                subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False)


if __name__ == "__main__":
    unittest.main()
