"""Exercise the CLI and real provider configs without network submissions."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'pastebinit'
CREATE_URL = 'https://pastebin.ca/api/v1/pastes'
CHALLENGE_URL = CREATE_URL + '/pow-challenge'


def response(body, url=CREATE_URL):
    result = io.BytesIO(body.encode('utf-8'))
    result.url = url
    return result


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.challenges = []
        self.submissions = []

    def pastebin_ca(self, req, data=None):
        self.assertTrue(req.get_header('User-agent').startswith('Pastebinit v'))
        if data is None:
            self.assertEqual(req.full_url, CHALLENGE_URL)
            challenge = {
                'challenge_id': 'challenge-%d' % len(self.challenges),
                'prefix': 'pastebinit-test-%d:' % len(self.challenges),
                'difficulty_bits': 8,
                'expires_at': 9999999999999,
            }
            self.challenges.append(challenge)
            return response(json.dumps(challenge), CHALLENGE_URL)

        self.assertEqual(req.full_url, CREATE_URL)
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        self.assertIsNone(req.get_header('Authorization'))
        payload = json.loads(data)
        challenge = self.challenges[-1]
        self.assertEqual(payload['pow_challenge_id'], challenge['challenge_id'])
        nonce = payload['pow_nonce']
        self.assertIsInstance(nonce, str)
        self.assertTrue(nonce.isdecimal())
        digest = hashlib.sha256((challenge['prefix'] + nonce).encode()).digest()
        self.assertEqual(digest[0], 0)
        self.submissions.append(payload)
        url = 'https://pastebin.ca/%dAbCdEfgHj' % (len(self.submissions) + 1)
        # raw_url deliberately comes first: stdout must contain the view URL.
        return response(json.dumps({'raw_url': url + '/raw', 'url': url}))

    def run_cli(self, arguments, content='', handler=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        real_open = open

        def without_user_config(path, *args, **kwargs):
            # Machine/user preferences must not change provider-default tests.
            if str(path).endswith('pastebinit.xml'):
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        with mock.patch('sys.argv', ['pastebinit'] + arguments), \
                mock.patch('sys.stdin', io.StringIO(content)), \
                mock.patch('builtins.open', side_effect=without_user_config), \
                mock.patch('urllib.request.urlopen',
                           side_effect=handler or self.pastebin_ca), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(SCRIPT), run_name='__main__')
                code = 0
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_anonymous_stdin_uses_json_defaults_and_prints_view_url(self):
        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca'], 'Harmless test: café ☕\nsecond line\n')
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout, 'https://pastebin.ca/2AbCdEfgHj\n')
        self.assertEqual(len(self.challenges), 1)
        payload = self.submissions[0]
        self.assertEqual(payload['body'], 'Harmless test: café ☕\nsecond line')
        self.assertEqual(payload['title'], '')
        self.assertEqual(payload['syntax_hint'], 'text')
        self.assertEqual(payload['expires_in'], '7d')
        self.assertEqual(payload['visibility'], 'unlisted')
        self.assertEqual(payload['access'], 'public')

    def test_each_input_file_gets_a_fresh_challenge_and_cli_options(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / 'one.txt', Path(directory) / 'two.txt']
            files[0].write_text('first file\n')
            files[1].write_text('second file\n')
            code, stdout, stderr = self.run_cli([
                '-b', 'https://pastebin.ca/', '-t', 'A test title',
                '-f', 'python', '-e', '30m', '-P', 'public',
                '-i', *map(str, files)])
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout.splitlines(), [
            'https://pastebin.ca/2AbCdEfgHj',
            'https://pastebin.ca/3AbCdEfgHj'])
        self.assertEqual(len(self.challenges), 2)
        self.assertEqual([p['body'] for p in self.submissions],
                         ['first file', 'second file'])
        self.assertNotEqual(self.submissions[0]['pow_challenge_id'],
                            self.submissions[1]['pow_challenge_id'])
        for payload in self.submissions:
            self.assertEqual(payload['title'], 'A test title')
            self.assertEqual(payload['syntax_hint'], 'python')
            self.assertEqual(payload['expires_in'], '30m')
            self.assertEqual(payload['visibility'], 'public')

    def test_invalid_challenge_difficulty_does_not_submit(self):
        for difficulty in (-1, 257, '22', True):
            with self.subTest(difficulty=difficulty):
                def bad_challenge(req, data=None):
                    self.assertIsNone(data)
                    return response(json.dumps({
                        'challenge_id': 'bad', 'prefix': 'test',
                        'difficulty_bits': difficulty}))

                code, stdout, stderr = self.run_cli(
                    ['-b', 'pastebin.ca'], 'test', bad_challenge)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, '')
                self.assertIn('Invalid proof-of-work difficulty', stderr)

    def test_work_is_bounded_and_timeout_does_not_submit(self):
        with mock.patch('time.monotonic', side_effect=[0, 31]):
            code, stdout, stderr = self.run_cli(['-b', 'pastebin.ca'], 'test')
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('exceeded 30 seconds', stderr)
        self.assertEqual(self.submissions, [])

    def test_http_rejection_does_not_print_a_success_url(self):
        def rejected(req, data=None):
            if data is None:
                return self.pastebin_ca(req, data)
            raise HTTPError(req.full_url, 400, 'Bad Request', {},
                            io.BytesIO(b'{"error":"invalid_expiry"}'))

        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca', '-e', 'bad'], 'test', rejected)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('HTTP Error', stderr)

    def test_response_without_view_url_does_not_print_raw_url(self):
        def missing_url(req, data=None):
            if data is None:
                return self.pastebin_ca(req, data)
            return response('{"raw_url":"https://pastebin.ca/2AbCdEfgHj/raw"}')

        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca'], 'test', missing_url)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('Unable to read or parse', stderr)

    def test_existing_json_provider_keeps_its_header_and_string_fields(self):
        def bpa_st(req, data=None):
            self.assertEqual(req.full_url, 'https://bpa.st/api/v1/paste')
            self.assertEqual(req.get_header('Content-type'), 'text/json')
            self.assertEqual(json.loads(data), {
                'name': '', 'content': 'café', 'lexer': 'text',
                'expiry': '1month'})
            return response('https://bpa.st/example')

        code, stdout, stderr = self.run_cli(['-b', 'bpa.st'], 'café', bpa_st)
        self.assertEqual((code, stdout, stderr),
                         (0, 'https://bpa.st/example\n', ''))

    def test_existing_form_provider_keeps_form_encoding_and_redirect_url(self):
        def debian(req, data=None):
            self.assertEqual(req.full_url, 'https://paste.debian.net/')
            self.assertIsNone(req.get_header('Content-type'))
            payload = parse_qs(data.decode('ascii'))
            self.assertEqual(payload['code'], ['café'])
            self.assertEqual(payload['private'], ['1'])
            self.assertEqual(payload['expire'], ['604800'])
            self.assertNotIn('pow_nonce', payload)
            return response('', 'https://paste.debian.net/12345/')

        code, stdout, stderr = self.run_cli(
            ['-b', 'paste.debian.net', '-a', 'test-user'], 'café', debian)
        self.assertEqual((code, stdout, stderr),
                         (0, 'https://paste.debian.net/12345/\n', ''))


if __name__ == '__main__':
    unittest.main()
