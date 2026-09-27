import email.message
import importlib.util
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("rhombus_player.py")
SPEC = importlib.util.spec_from_file_location("rhombus_player", MODULE_PATH)
rhombus_player = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rhombus_player
assert SPEC.loader is not None
SPEC.loader.exec_module(rhombus_player)


class ParserTests(unittest.TestCase):
    def test_parses_player_card_and_attachment(self):
        html = """
        <ul><li class="bg-card border-l-4 p-4">
          <button hx-get="/challenges/example-id">
            <span>Crypto /</span><span> Example Name </span>
          </button>
          <span>\u20680\u2069 solves / \u2068250\u2069 pts</span>
          <div class="prose dark:prose-invert">Recover the <code>secret</code>.</div>
          <a href="/uploads/hash-capture.bin"><svg><path></path></svg>capture.bin</a>
          <a href="https://cdn.example/evidence.zip"><svg class="lucide lucide-paperclip"></svg>evidence.zip</a>
        </li></ul>
        """
        challenges = rhombus_player.parse_challenges_html(html, "https://ctf.example")
        self.assertEqual(len(challenges), 1)
        challenge = challenges[0]
        self.assertEqual(challenge.id, "example-id")
        self.assertEqual(challenge.name, "Example Name")
        self.assertEqual(challenge.category, "Crypto")
        self.assertEqual(challenge.points, 250)
        self.assertEqual(challenge.description, "Recover the secret .")
        self.assertEqual(len(challenge.attachments), 2)
        self.assertEqual(challenge.attachments[0].name, "capture.bin")
        self.assertEqual(
            challenge.attachments[0].url,
            "https://ctf.example/uploads/hash-capture.bin",
        )
        self.assertEqual(challenge.attachments[1].url, "https://cdn.example/evidence.zip")

    def test_rejects_page_without_challenges(self):
        with self.assertRaises(rhombus_player.ParseError):
            rhombus_player.parse_challenges_html("<html>sign in</html>", "https://ctf.example")

    def test_attachment_filename_cannot_escape_output(self):
        self.assertEqual(rhombus_player._safe_filename("../../answer.txt"), "answer.txt")
        self.assertEqual(rhombus_player._safe_filename(r"..\\..\\answer.txt"), "answer.txt")


class RateLimitTests(unittest.TestCase):
    @staticmethod
    def rate_limit_error(retry_after="0"):
        headers = email.message.Message()
        headers["Retry-After"] = retry_after
        headers["X-RateLimit-After"] = retry_after
        return urllib.error.HTTPError(
            "https://ctf.example/challenges", 429, "Too Many Requests", headers, None
        )

    def test_retries_429_using_server_header_and_backoff_floor(self):
        marker = object()
        notices = []
        api = rhombus_player.RhombusAPI(
            "https://ctf.example",
            max_retries=2,
            on_rate_limit=lambda wait, attempt, maximum: notices.append((wait, attempt, maximum)),
        )
        with mock.patch.object(
            rhombus_player.urllib.request,
            "urlopen",
            side_effect=[self.rate_limit_error("0"), marker],
        ), mock.patch.object(rhombus_player.time, "sleep") as sleep:
            self.assertIs(api._request("/challenges"), marker)
        sleep.assert_called_once_with(0.55)
        self.assertEqual(notices, [(0.55, 1, 2)])

    def test_raises_after_bounded_retries(self):
        api = rhombus_player.RhombusAPI("https://ctf.example", max_retries=1)
        with mock.patch.object(
            rhombus_player.urllib.request,
            "urlopen",
            side_effect=[self.rate_limit_error("1"), self.rate_limit_error("1")],
        ), mock.patch.object(rhombus_player.time, "sleep") as sleep:
            with self.assertRaises(rhombus_player.RateLimitError) as caught:
                api._request("/challenges")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertAlmostEqual(caught.exception.retry_after, 1.05)
        sleep.assert_called_once_with(1.05)


class SessionTests(unittest.TestCase):
    def test_session_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            rhombus_player.save_session(path, "https://ctf.example", "alice", "secret-token")
            self.assertEqual(
                rhombus_player.load_session(path),
                {
                    "base_url": "https://ctf.example",
                    "username": "alice",
                    "token": "secret-token",
                },
            )


if __name__ == "__main__":
    unittest.main()
