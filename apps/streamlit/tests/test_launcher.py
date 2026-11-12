import importlib.util
import contextlib
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('streamlit_launcher',ROOT/'scripts/launch.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


class Launcher(unittest.TestCase):
    def test_bundle_has_source_without_runtime_secrets_or_local_connections(self):
        data=launcher.package()
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            names=archive.getnames()
        self.assertIn('app.py',names)
        self.assertIn('requirements.txt',names)
        self.assertIn('.streamlit/config.toml',names)
        self.assertFalse(any('.runtime' in p or '.venv' in p or p.endswith('.local.json') or p.endswith('.env') for p in names))

    def test_missing_dependency_manifest_fails_before_upload(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(launcher, 'ROOT', Path(folder)):
            with self.assertRaisesRegex(RuntimeError, 'missing: .*requirements.txt'):
                launcher.package()

    def test_build_failure_saves_private_diagnostics_without_echoing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder)/'private/build.log'
            result = subprocess.CompletedProcess(['ssh'], 1, b'private-build-output', b'build failed')
            with mock.patch.object(launcher.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError) as error:
                    launcher.run(['ssh'], error_log=log)
            self.assertIn(str(log), str(error.exception))
            self.assertNotIn('private-build-output', str(error.exception))
            self.assertIn(b'build failed', log.read_bytes())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)

    def test_remote_build_installs_only_a_missing_buildx_plugin(self):
        context = {'vllm': {'private_ip': {'value': '10.0.0.1'}},
                   'monitoring': {'grafana_url': {'value': 'https://grafana.example'}}}
        calls = []
        with mock.patch.object(launcher, 'run', side_effect=lambda argv, **kw: calls.append(argv)):
            launcher.install_monitoring(['ssh'], context)
        remote = shlex.split(next(call[-1] for call in calls if call[-1].startswith('bash -c ')))[2]
        for present in (False, True):
            with self.subTest(plugin_present=present), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                binaries = root/'bin'
                binaries.mkdir()
                scripts = {
                    'sudo': '#!/bin/sh\nexec "$@"\n',
                    'apt-get': '#!/bin/sh\necho "$*" >> "$DOCKER_TEST_DIR/apt.log"\n'
                               'if [ "$1" = install ]; then touch "$DOCKER_TEST_DIR/installed"; fi\n',
                    'docker': '#!/bin/sh\n'
                              'if [ "$1" = buildx ]; then test -f "$DOCKER_TEST_DIR/installed"; exit $?; fi\n'
                              'if [ "$1" = image ]; then echo python@sha256:fixture; fi\n'
                              'if [ "$1 $2" = "compose build" ]; then test -f "$DOCKER_TEST_DIR/installed"; exit $?; fi\n'
                              'exit 0\n',
                }
                for name, content in scripts.items():
                    path = binaries/name
                    path.write_text(content)
                    path.chmod(0o755)
                if present:
                    (root/'installed').touch()
                environment = dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'],
                                   DOCKER_TEST_DIR=folder)
                script = remote.replace('cd /opt/vllm-streamlit-client', 'cd '+shlex.quote(folder))
                result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual((root/'.env').read_text(), 'PYTHON_IMAGE=python@sha256:fixture\n')
                self.assertEqual((root/'apt.log').exists(), not present)
                if not present:
                    self.assertIn('docker-buildx', (root/'apt.log').read_text())

    def test_image_normalizes_restrictive_source_permissions_before_nonroot_start(self):
        dockerfile=(ROOT/'Dockerfile').read_text()
        normalize=dockerfile.split('RUN chmod -R ',1)[1].split('USER ',1)[0].strip()
        command='chmod -R '+normalize.replace('\\\n','')
        self.assertIn('USER 10001:10001',dockerfile)
        with tempfile.TemporaryDirectory() as folder:
            app=Path(folder)
            for name in ('client/nested/module.py','.streamlit/config.toml','app.py','requirements.txt'):
                p=app/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture');p.chmod(0o600)
            for name in ('client','client/nested','.streamlit'):(app/name).chmod(0o700)
            subprocess.run(['bash','-c',command.replace('/app/',shlex.quote(folder)+'/')],check=True)
            for p in app.rglob('*'):
                mode=p.stat().st_mode & 0o777
                self.assertEqual(mode & 0o004,0o004,str(p))
                if p.is_dir():self.assertEqual(mode & 0o001,0o001,str(p))
                else:self.assertEqual(mode & 0o222,0,str(p))

    def test_readiness_waits_for_http_health_after_container_creation(self):
        state=subprocess.CompletedProcess([],0,json.dumps({'Status':'running','ExitCode':0}),'')
        response=mock.MagicMock();response.__enter__.return_value.status=200
        with mock.patch('subprocess.run',return_value=state), \
             mock.patch('urllib.request.urlopen',side_effect=[OSError('starting'),response]) as request, \
             mock.patch('time.sleep') as sleep,contextlib.redirect_stdout(io.StringIO()):
            exec(launcher.monitoring_readiness_program(),{})
        self.assertEqual(request.call_count,2);sleep.assert_called_once_with(2)

    def test_readiness_fails_immediately_for_crash_loop_without_printing_container_logs(self):
        for status in ('restarting','exited','dead'):
            state=subprocess.CompletedProcess([],0,json.dumps({'Status':status,'ExitCode':1}),'')
            with self.subTest(status=status),mock.patch('subprocess.run',return_value=state) as docker, \
                 mock.patch('urllib.request.urlopen') as request:
                with self.assertRaisesRegex(SystemExit,'failed during startup'):
                    exec(launcher.monitoring_readiness_program(),{})
                request.assert_not_called()
                self.assertEqual(docker.call_args.args[0][1],'inspect')

    def test_readiness_times_out_when_running_container_never_serves_health(self):
        state=subprocess.CompletedProcess([],0,json.dumps({'Status':'running','ExitCode':0}),'')
        with mock.patch('subprocess.run',return_value=state), \
             mock.patch('urllib.request.urlopen',side_effect=OSError('not ready')), \
             mock.patch('time.monotonic',side_effect=[0,121]),mock.patch('time.sleep') as sleep:
            with self.assertRaisesRegex(SystemExit,'within 120 seconds'):
                exec(launcher.monitoring_readiness_program(),{})
            sleep.assert_not_called()

    def test_failed_readiness_does_not_report_app_healthy(self):
        context={'vllm':{'private_ip':{'value':'10.0.0.1'}},
                 'monitoring':{'grafana_url':{'value':'https://grafana.example'}}}
        def run(argv,**kw):
            if kw.get('data')==launcher.monitoring_readiness_program().encode():
                raise RuntimeError('private startup diagnostics')
        output=io.StringIO()
        with mock.patch.object(launcher,'run',side_effect=run),contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError,'private startup'):
                launcher.install_monitoring(['ssh'],context)
        self.assertNotIn('Monitoring app healthy',output.getvalue())

    def test_cleanup_removes_its_key_and_stops_only_owned_process(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key'
            launcher.private_write(key,'test-value')
            process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
            try:
                launcher.PROCESSES.append(process)
                launcher.CREDENTIALS.append(key)
                launcher.cleanup()
                self.assertIsNotNone(process.poll())
                self.assertFalse(key.exists())
            finally:
                launcher.PROCESSES.clear();launcher.CREDENTIALS.clear()
                if process.poll() is None:process.kill();process.wait()

    def test_collision_is_reported_without_touching_other_listener(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen()
            with self.assertRaisesRegex(RuntimeError,'already in use'):
                launcher.require_free(s.getsockname()[1])
            self.assertGreater(s.fileno(),0)
