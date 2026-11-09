"""Exercise the CLI and real provider configs without network submissions."""

import contextlib
from email.message import Message
import gettext
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import socket
import tempfile
import unittest
from unittest import mock
from urllib import request
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.response import addinfourl


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'pastebinit'
CREATE_URL = 'https://pastebin.ca/api/v1/pastes'
CHALLENGE_URL = CREATE_URL + '/pow-challenge'
TEST_TOKEN = 'pbca_live_pastebinit_test_token'


def response(body, url=CREATE_URL, code=200, headers=None):
    fields = Message()
    for name, value in (headers or {}).items():
        fields[name] = value
    result = addinfourl(io.BytesIO(body.encode('utf-8')), fields, url, code)
    result.msg = 'Test response'
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
                'difficulty_bits': 13,
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
        bits = ''.join(format(byte, '08b') for byte in digest)
        self.assertTrue(bits.startswith('0' * challenge['difficulty_bits']))
        self.submissions.append(payload)
        url = 'https://pastebin.ca/%dAbCdEfgHj' % (len(self.submissions) + 1)
        # raw_url deliberately comes first: stdout must contain the view URL.
        return response(json.dumps({'raw_url': url + '/raw', 'url': url}))

    def authenticated_pastebin_ca(self, req, data=None):
        self.assertEqual(req.full_url, CREATE_URL)
        self.assertEqual(req.get_header('Authorization'), 'Bearer ' + TEST_TOKEN)
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        self.assertIsNotNone(data)
        self.assertNotIn(TEST_TOKEN.encode(), data)
        payload = json.loads(data)
        self.assertNotIn('pow_challenge_id', payload)
        self.assertNotIn('pow_nonce', payload)
        self.submissions.append(payload)
        url = 'https://pastebin.ca/%dAbCdEfgHj' % (len(self.submissions) + 1)
        return response(json.dumps({'url': url}))

    def run_cli(self, arguments, content='', handler=None, env=None,
                provider_config=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        real_open = open
        real_listdir = os.listdir
        real_build_opener = request.build_opener
        handler = handler or self.pastebin_ca
        environment = dict(os.environ)
        environment.pop('PBCA_API_KEY', None)
        environment.update(env or {})

        def without_user_config(path, *args, **kwargs):
            # Machine/user preferences must not change provider-default tests.
            if str(path).endswith('pastebinit.xml'):
                raise FileNotFoundError(path)
            if provider_config is not None and \
                    Path(path) == ROOT / 'pastebin.d' / 'pastebin.ca.conf':
                return io.StringIO(provider_config)
            return real_open(path, *args, **kwargs)

        def without_external_providers(path):
            directory = Path(path)
            if directory.name.endswith('pastebin.d') and \
                    directory.resolve() != ROOT / 'pastebin.d':
                return []
            return real_listdir(path)

        class TestTransport(request.BaseHandler):
            def default_open(self, req):
                return handler(req, req.data)

        def offline_opener(*handlers):
            # Keep urllib's real redirect/error processing; replace only I/O.
            return real_build_opener(request.ProxyHandler({}),
                                     *handlers, TestTransport())

        with contextlib.ExitStack() as cleanup, \
                mock.patch('sys.argv', ['pastebinit'] + arguments), \
                mock.patch('sys.stdin', io.StringIO(content)), \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch('builtins.open', side_effect=without_user_config), \
                mock.patch('os.listdir', side_effect=without_external_providers), \
                mock.patch('gettext.gettext', new=lambda message: message), \
                mock.patch('urllib.request.urlopen',
                           side_effect=handler), \
                mock.patch('urllib.request.build_opener',
                           side_effect=offline_opener), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            cleanup.callback(socket.setdefaulttimeout,
                             socket.getdefaulttimeout())
            cleanup.callback(gettext.textdomain, gettext.textdomain())
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

    def test_empty_api_key_uses_anonymous_flow(self):
        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca'], 'test', env={'PBCA_API_KEY': ''})
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout, 'https://pastebin.ca/2AbCdEfgHj\n')
        self.assertEqual(len(self.challenges), 1)

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
        elapsed = 0
        real_sha256 = hashlib.sha256

        def slow_hash(data):
            nonlocal elapsed
            elapsed += 10
            return real_sha256(data)

        with mock.patch('time.monotonic', side_effect=lambda: elapsed), \
                mock.patch('hashlib.sha256', side_effect=slow_hash):
            code, stdout, stderr = self.run_cli(['-b', 'pastebin.ca'], 'test')
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('exceeded 30 seconds', stderr)
        self.assertGreater(elapsed, 0)
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

    def test_authenticated_stdin_uses_bearer_and_preserves_defaults(self):
        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca', '-V'], 'Authenticated café ☕\n',
            self.authenticated_pastebin_ca, {'PBCA_API_KEY': TEST_TOKEN})
        self.assertEqual(code, 0)
        self.assertEqual(stdout, 'https://pastebin.ca/2AbCdEfgHj\n')
        self.assertNotIn(TEST_TOKEN, stdout + stderr)
        self.assertEqual(self.challenges, [])
        self.assertEqual(self.submissions, [{
            'body': 'Authenticated café ☕', 'title': '',
            'syntax_hint': 'text', 'expires_in': '7d',
            'visibility': 'unlisted', 'access': 'public'}])

    def test_authenticated_files_keep_credentials_and_options(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / 'one.txt', Path(directory) / 'two.txt']
            for index, path in enumerate(files):
                path.write_text('file %d\n' % index)
            code, stdout, stderr = self.run_cli([
                '-b', 'pastebin.ca', '-t', 'Build output', '-f', 'log',
                '-e', '1d', '-P', 'public', '-i', *map(str, files)],
                handler=self.authenticated_pastebin_ca,
                env={'PBCA_API_KEY': TEST_TOKEN})
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout.splitlines(), [
            'https://pastebin.ca/2AbCdEfgHj',
            'https://pastebin.ca/3AbCdEfgHj'])
        self.assertEqual(self.challenges, [])
        self.assertEqual([p['body'] for p in self.submissions],
                         ['file 0', 'file 1'])
        for payload in self.submissions:
            self.assertEqual(payload['title'], 'Build output')
            self.assertEqual(payload['syntax_hint'], 'log')
            self.assertEqual(payload['expires_in'], '1d')
            self.assertEqual(payload['visibility'], 'public')

    def test_invalid_api_key_is_rejected_before_network_without_disclosure(self):
        for suffix in (' with-space', '\nheader', '\rheader', '\t', 'é'):
            with self.subTest(suffix=suffix):
                network = mock.Mock(side_effect=AssertionError('Unexpected I/O'))
                code, stdout, stderr = self.run_cli(
                    ['-b', 'pastebin.ca', '-V'], 'test', network,
                    {'PBCA_API_KEY': TEST_TOKEN + suffix})
                self.assertEqual(code, 1)
                self.assertEqual(stdout, '')
                self.assertNotIn(TEST_TOKEN, stderr)
                self.assertIn('token', stderr.lower())
                network.assert_not_called()

    def test_api_key_requires_https(self):
        config = (ROOT / 'pastebin.d' / 'pastebin.ca.conf').read_text()
        config = config.replace('https = True\n', '')
        network = mock.Mock(side_effect=AssertionError('Unexpected I/O'))
        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca', '-V'], 'test', network,
            {'PBCA_API_KEY': TEST_TOKEN}, config)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('HTTPS', stderr)
        self.assertNotIn(TEST_TOKEN, stderr)
        network.assert_not_called()

    def test_rejected_key_does_not_retry_anonymously_or_leak_from_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                attempts = []

                def rejected(req, data=None):
                    attempts.append(req.full_url)
                    self.assertEqual(req.full_url, CREATE_URL)
                    self.assertEqual(req.get_header('Authorization'),
                                     'Bearer ' + TEST_TOKEN)
                    raise HTTPError(req.full_url, status,
                                    'Rejected ' + TEST_TOKEN, {},
                                    io.BytesIO(json.dumps({
                                        'error': 'invalid_token',
                                        'received': TEST_TOKEN}).encode()))

                code, stdout, stderr = self.run_cli(
                    ['-b', 'pastebin.ca', '-V'], 'test', rejected,
                    {'PBCA_API_KEY': TEST_TOKEN})
                self.assertEqual(code, 1)
                self.assertEqual(attempts, [CREATE_URL])
                self.assertNotIn(TEST_TOKEN, stdout + stderr)
                self.assertNotIn('https://pastebin.ca/', stdout)
                self.assertIn(str(status), stderr)

    def test_transport_error_does_not_disclose_key(self):
        def failed(req, data=None):
            raise OSError('Connection failed for ' + TEST_TOKEN)

        code, stdout, stderr = self.run_cli(
            ['-b', 'pastebin.ca', '-V'], 'test', failed,
            {'PBCA_API_KEY': TEST_TOKEN})
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertNotIn(TEST_TOKEN, stderr)
        self.assertIn('Failed to contact', stderr)

    def test_authenticated_redirects_are_rejected_without_following(self):
        destinations = ['https://pastebin.ca/redirect',
                        'https://other.invalid/paste',
                        'http://pastebin.ca/redirect']
        for status in (301, 302, 303, 307, 308):
            for destination in destinations:
                with self.subTest(status=status, destination=destination):
                    attempts = []

                    def redirected(req, data=None):
                        attempts.append(req.full_url)
                        self.assertEqual(req.full_url, CREATE_URL)
                        self.assertEqual(req.get_header('Authorization'),
                                         'Bearer ' + TEST_TOKEN)
                        return response('', code=status,
                                        headers={'Location': destination})

                    code, stdout, stderr = self.run_cli(
                        ['-b', 'pastebin.ca', '-V'], 'test', redirected,
                        {'PBCA_API_KEY': TEST_TOKEN})
                    self.assertEqual(code, 1)
                    self.assertEqual(attempts, [CREATE_URL])
                    self.assertNotIn(TEST_TOKEN, stdout + stderr)
                    self.assertNotIn('https://pastebin.ca/', stdout)
                    self.assertIn('redirect', stderr.lower())

    def test_existing_json_provider_keeps_its_header_and_string_fields(self):
        def bpa_st(req, data=None):
            self.assertEqual(req.full_url, 'https://bpa.st/api/v1/paste')
            self.assertEqual(req.get_header('Content-type'), 'text/json')
            self.assertIsNone(req.get_header('Authorization'))
            self.assertEqual(json.loads(data), {
                'name': '', 'content': 'café', 'lexer': 'text',
                'expiry': '1month'})
            return response('https://bpa.st/example')

        code, stdout, stderr = self.run_cli(
            ['-b', 'bpa.st'], 'café', bpa_st,
            {'PBCA_API_KEY': TEST_TOKEN})
        self.assertEqual((code, stdout, stderr),
                         (0, 'https://bpa.st/example\n', ''))

    def test_existing_form_provider_keeps_form_encoding_and_redirect_url(self):
        def debian(req, data=None):
            self.assertEqual(req.full_url, 'https://paste.debian.net/')
            self.assertIsNone(req.get_header('Content-type'))
            self.assertIsNone(req.get_header('Authorization'))
            payload = parse_qs(data.decode('ascii'))
            self.assertEqual(payload['code'], ['café'])
            self.assertEqual(payload['private'], ['1'])
            self.assertEqual(payload['expire'], ['604800'])
            self.assertNotIn('pow_nonce', payload)
            return response('', 'https://paste.debian.net/12345/')

        code, stdout, stderr = self.run_cli(
            ['-b', 'paste.debian.net', '-a', 'test-user'], 'café', debian,
            {'PBCA_API_KEY': TEST_TOKEN})
        self.assertEqual((code, stdout, stderr),
                         (0, 'https://paste.debian.net/12345/\n', ''))


if __name__ == '__main__':
    unittest.main()
