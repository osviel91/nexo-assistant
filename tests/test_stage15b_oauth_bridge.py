import unittest

from prototypes.oauth_bridge import PairingBridge, PairingError


class PairingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = PairingBridge("https://nexo.example.test")

    def start(self):
        return self.bridge.begin("operator-1", now=100)

    def test_transfer_waits_for_authorized_confirmation_and_is_single_use(self):
        session, code = self.start()
        credentials = {"access_token": "FAKE_ACCESS", "refresh_token": "FAKE_REFRESH"}
        self.bridge.transfer(session, code, "https://nexo.example.test/bridge/transfer", credentials, 101)
        with self.assertRaises(PairingError):
            self.bridge.confirm(session, "operator-2", 102)
        self.assertEqual(self.bridge.confirm(session, "operator-1", 102), credentials)
        with self.assertRaises(PairingError):
            self.bridge.confirm(session, "operator-1", 103)

    def test_rejects_missing_authorized_session(self):
        with self.assertRaises(PairingError):
            self.bridge.begin("", now=100)

    def test_rejects_wrong_code_and_replay(self):
        session, code = self.start()
        with self.assertRaises(PairingError):
            self.bridge.transfer(session, "wrong", "https://nexo.example.test/transfer", {}, 101)
        self.bridge.transfer(session, code, "https://nexo.example.test/transfer", {"access_token": "FAKE"}, 101)
        with self.assertRaises(PairingError):
            self.bridge.transfer(session, code, "https://nexo.example.test/transfer", {"access_token": "FAKE"}, 102)

    def test_expires_pairing(self):
        session, code = self.start()
        with self.assertRaises(PairingError):
            self.bridge.transfer(session, code, "https://nexo.example.test/transfer", {}, 400)

    def test_rejects_untrusted_or_credential_bearing_destination(self):
        session, code = self.start()
        for destination in (
            "http://nexo.example.test/transfer",
            "https://attacker.example.test/transfer",
            "https://user@nexo.example.test/transfer",
            "https://nexo.example.test/transfer?access_token=leak",
        ):
            with self.subTest(destination=destination), self.assertRaises(PairingError):
                self.bridge.transfer(session, code, destination, {}, 101)

    def test_diagnostics_expose_no_pairing_or_credential_data(self):
        session, code = self.start()
        credentials = {"access_token": "FAKE_ACCESS", "refresh_token": "FAKE_REFRESH"}
        self.bridge.transfer(session, code, "https://nexo.example.test/transfer", credentials, 101)
        diagnostics = repr(self.bridge.diagnostics())
        for secret in (session, code, *credentials.values()):
            self.assertNotIn(secret, diagnostics)
        self.assertEqual(self.bridge.diagnostics(), {"pending_pairings": 1})


if __name__ == "__main__":
    unittest.main()
