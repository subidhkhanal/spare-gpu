import os
import unittest

os.environ.setdefault("NOTESVC_SECRET", "test-secret")

from notesvc import auth  # noqa: E402


class AuthTest(unittest.TestCase):
    def test_password_and_session(self):
        salt, digest = auth.hash_password("pw")
        self.assertTrue(auth.check_password("pw", salt, digest))
        self.assertFalse(auth.check_password("no", salt, digest))
        self.assertEqual(auth.read_session(auth.make_session("ada")), {"user": "ada"})
        self.assertIsNone(auth.read_session(auth.make_session("ada") + "x"))


if __name__ == "__main__":
    unittest.main()
