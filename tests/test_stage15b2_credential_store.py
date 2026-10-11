import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from prototypes.credential_store import CredentialStore, StoreError


SYNTHETIC = {"client_id": "FAKE_CLIENT", "access_token": "FAKE_ACCESS_TOKEN",
             "refresh_token": "FAKE_REFRESH_TOKEN", "subject": "FAKE_SUBJECT"}


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "credentials.sqlite3"
        self.key = self.root / "external.key"
        self.key.write_bytes(os.urandom(32))
        self.key.chmod(0o600)
        self.store = CredentialStore(str(self.db), str(self.key))
        self.transaction = str(uuid.uuid4())
        self.store.authorize_import(self.transaction, 400, "admin", 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_authorized_stage_is_encrypted_pending_and_survives_restart(self):
        credential_id, fingerprint = self.store.stage(self.transaction, SYNTHETIC, 101)
        self.assertEqual(self.store.status(credential_id, 102), {"status": "pending", "fingerprint": fingerprint})
        self.assertNotIn(b"FAKE_ACCESS_TOKEN", self.db.read_bytes())
        self.store = CredentialStore(str(self.db), str(self.key))
        with self.assertRaises(StoreError):
            self.store.get_credentials(credential_id, "credential-provider", 102)
        self.store.confirm(credential_id, fingerprint, "admin")
        self.store = CredentialStore(str(self.db), str(self.key))
        self.assertEqual(self.store.get_credentials(credential_id, "credential-provider", 102), SYNTHETIC)

    def test_import_requires_admin_and_authorized_single_use_transaction(self):
        with self.assertRaises(StoreError):
            self.store.authorize_import(str(uuid.uuid4()), 400, "intruder", 100)
        with self.assertRaises(StoreError):
            self.store.stage(str(uuid.uuid4()), SYNTHETIC, 101)
        self.store.stage(self.transaction, SYNTHETIC, 101)
        with self.assertRaises(StoreError):
            self.store.stage(self.transaction, SYNTHETIC, 101)

    def test_confirmation_fingerprint_and_admin_are_required(self):
        credential_id, fingerprint = self.store.stage(self.transaction, SYNTHETIC, 101)
        with self.assertRaises(StoreError):
            self.store.confirm(credential_id, fingerprint, "intruder")
        with self.assertRaises(StoreError):
            self.store.confirm(credential_id, "0" * 64, "admin")
        self.store.confirm(credential_id, fingerprint, "admin")

    def test_only_authorized_component_can_read_and_delete_blocks_access(self):
        credential_id, fingerprint = self.store.stage(self.transaction, SYNTHETIC, 101)
        self.store.confirm(credential_id, fingerprint, "admin")
        with self.assertRaises(StoreError):
            self.store.get_credentials(credential_id, "web", 102)
        self.store.delete(credential_id, "admin")
        with self.assertRaises(StoreError):
            self.store.get_credentials(credential_id, "credential-provider", 102)
        self.assertEqual(self.store.status(credential_id, 102), {"status": "absent"})

    def test_ciphertext_tampering_and_wrong_key_fail_closed(self):
        credential_id, fingerprint = self.store.stage(self.transaction, SYNTHETIC, 101)
        self.store.confirm(credential_id, fingerprint, "admin")
        with self.store._connect() as db:
            ciphertext = db.execute("SELECT ciphertext FROM credentials WHERE id=?", (credential_id,)).fetchone()[0]
            db.execute("UPDATE credentials SET ciphertext=? WHERE id=?", (ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), credential_id))
        with self.assertRaises(StoreError):
            self.store.get_credentials(credential_id, "credential-provider", 102)
        self.key.write_bytes(os.urandom(32))
        with self.assertRaises(StoreError):
            CredentialStore(str(self.db), str(self.key)).get_credentials(credential_id, "credential-provider", 102)

    def test_malformed_fields_expiry_and_diagnostics_do_not_disclose_secrets(self):
        with self.assertRaises(StoreError):
            self.store.stage(self.transaction, SYNTHETIC | {"unexpected": "value"}, 101)
        credential_id, _ = self.store.stage(self.transaction, SYNTHETIC | {"expires_at": 101}, 101)
        self.store.confirm(credential_id, self.store.status(credential_id, 101)["fingerprint"], "admin")
        with self.assertRaises(StoreError):
            self.store.get_credentials(credential_id, "credential-provider", 101)
        self.assertNotIn("FAKE_", json.dumps(self.store.diagnostics()))


if __name__ == "__main__":
    unittest.main()
