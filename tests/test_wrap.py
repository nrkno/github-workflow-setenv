"""Exercise the workflow's actual wrap step with a local fake Vault."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/workflow.yaml'


def wrap_script():
    # Take the literal YAML run block from the identified step, not a copy of its script.
    step = WORKFLOW.read_text(encoding='utf-8').split('        id: wrap_outputs\n', 1)[1]
    block = step.split('        run: |\n', 1)[1]
    script = []
    for line in block.splitlines():
        if line and not line.startswith('          '):
            break
        script.append(line[10:])
    return '\n'.join(script) + '\n'


class FakeVault(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests.append((self.path, self.headers, body))
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        response = self.server.response
        self.wfile.write(response if isinstance(response, bytes) else json.dumps(response).encode())


class WrapTest(unittest.TestCase):
    def run_wrap(self, status, response, ttl='300'):
        payload = '{"TF_VAR_example":"50% complete\\nsecond line"}'
        with tempfile.TemporaryDirectory() as temp, HTTPServer(('127.0.0.1', 0), FakeVault) as server:
            server.status = status
            server.response = response
            server.requests = []
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                runner_temp = Path(temp)
                out = runner_temp / 'output'
                env = {
                    'PATH': os.environ['PATH'],
                    'RUNNER_TEMP': temp,
                    'GITHUB_OUTPUT': str(out),
                    'VAULT_ADDR': f'http://127.0.0.1:{server.server_port}',
                    'VAULT_TOKEN': 'test-login-token',
                    'WRAP_TTL': ttl,
                    'SETENV_OUTPUTS': payload,
                }
                proc = subprocess.run(
                    ['bash', '-c', wrap_script()], env=env, cwd=temp,
                    capture_output=True, text=True, timeout=10, check=False,
                )
                output = out.read_text(encoding='utf-8') if out.exists() else ''
                self.assertEqual(list(runner_temp.glob('wrap.*')), [])
                self.assertNotIn(payload, proc.stdout + proc.stderr)
                self.assertNotIn(env['VAULT_TOKEN'], proc.stdout + proc.stderr)
                return proc, output, server.requests, payload
            finally:
                server.shutdown()
                thread.join()

    def test_sends_payload_unchanged_and_returns_only_the_wrap_token(self):
        proc, output, requests, payload = self.run_wrap(200, {'wrap_info': {'token': 'test-wrap-token'}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(requests), 1)
        path, headers, body = requests[0]
        self.assertEqual(path, '/v1/sys/wrapping/wrap')
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['X-Vault-Wrap-TTL'], '300')
        self.assertEqual(headers['X-Vault-Token'], 'test-login-token')
        self.assertEqual(body, payload.encode())
        self.assertEqual(output, 'wrap_token=test-wrap-token\n')
        self.assertEqual(proc.stdout + proc.stderr, '')

    def test_percent_in_error_detail_survives_runner_decoding_as_literal_text(self):
        proc, output, _, _ = self.run_wrap(403, {'errors': ['50% complete\r\nliteral %0A and %0D']})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(output, '')
        self.assertEqual(
            proc.stderr,
            '::error::Vault wrap request returned HTTP 403: 50%25 complete  literal %250A and %250D\n',
        )

    def test_missing_or_malformed_token_is_an_error(self):
        for token in (None, '', False, 123, 'test-wrap-token\r\n', 'one\ntwo', 'one\0two'):
            with self.subTest(token=token):
                proc, output, _, _ = self.run_wrap(200, {'wrap_info': {'token': token}})
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(output, '')
                self.assertIn('Vault response had no valid wrap_info.token', proc.stderr)

    def test_multiple_json_objects_cannot_produce_multiple_output_lines(self):
        response = b'{"wrap_info":{"token":"first"}}\n{"wrap_info":{"token":"second"}}'
        proc, output, _, _ = self.run_wrap(200, response)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(output, '')

    def test_invalid_ttl_fails_without_requesting_vault(self):
        proc, output, requests, _ = self.run_wrap(200, {}, ttl='five minutes')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(output, '')
        self.assertEqual(requests, [])
        self.assertIn('wrap-ttl must be a number of seconds', proc.stderr)
