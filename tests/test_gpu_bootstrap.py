"""APT contention and conservative repair/health verification, without cloud writes."""
from pathlib import Path
from types import SimpleNamespace
import contextlib
import errno
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import gpu_bootstrap as gpu
import hosts
loader=importlib.machinery.SourceFileLoader('poc_apt',str(ROOT/'terraform/vllm/apt-retry.py.tftpl'))
spec=importlib.util.spec_from_loader(loader.name,loader);apt=importlib.util.module_from_spec(spec);loader.exec_module(apt)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.expected={'instance_id':'ocid1.instance.fixture','model_revision':'a'*40,
                       'mount_source':'10.0.1.2:/models','vllm_image':'fixture-image','bootstrap_sha256':hashlib.sha256(b'fixture').hexdigest()}
        self.errors=["('package_update_upgrade_install', TimeoutError('Could not get APT lock'))",
                     "('scripts_user', RuntimeError('Runparts: 1 failures (runcmd) in 1 attempted commands'))"]
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)

    def test_apt_waits_for_existing_lock_then_installs_noninteractively(self):
        now=[0];run=mock.Mock(return_value=SimpleNamespace(returncode=0,stdout='installed\n'))
        apt.apt(['install','-y','docker.io'],busy=mock.Mock(side_effect=[True,True,False]),
                run=run,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        self.assertEqual(now[0],10)
        self.assertIn('DPkg::Lock::Timeout=30',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env']['DEBIAN_FRONTEND'],'noninteractive')

    def test_lock_timeout_never_runs_apt_or_removes_lock(self):
        now=[0];run=mock.Mock()
        with self.assertRaises(TimeoutError):
            apt.apt(['update'],timeout=10,busy=lambda:True,run=run,
                clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        run.assert_not_called();self.assertEqual(now[0],10)

    def test_update_lock_race_retries_even_after_owner_releases_lock(self):
        now=[0];run=mock.Mock(side_effect=[SimpleNamespace(returncode=100,stdout='Could not get lock /var/lib/apt/lists/lock'),SimpleNamespace(returncode=0,stdout='')])
        apt.apt(['update'],busy=lambda:False,run=run,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        self.assertEqual(run.call_count,2)

    def test_non_lock_package_failure_stops_without_repeated_install(self):
        run=mock.Mock(return_value=SimpleNamespace(returncode=100,stdout='Unable to locate package missing'))
        with self.assertRaises(RuntimeError):apt.apt(['install','missing'],busy=lambda:False,run=run)
        self.assertEqual(run.call_count,1)

    def test_lock_probe_does_not_truncate_or_unlink_existing_file(self):
        p=self.root/'lock';p.write_text('preserve-lock-content')
        with mock.patch.object(apt.fcntl,'lockf',side_effect=BlockingIOError(errno.EAGAIN,'busy')):
            self.assertTrue(apt.locked([p]))
        self.assertEqual(p.read_text(),'preserve-lock-content')
        self.assertFalse(apt.locked([self.root/'absent']))

    def test_real_lock_owner_is_left_running_and_lock_content_is_preserved(self):
        path=self.root/'real-lock';path.write_text('preserve')
        child=subprocess.Popen([sys.executable,'-c',
            'import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.lockf(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.readline()',
            str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            self.assertTrue(apt.locked([path]));self.assertIsNone(child.poll())
            self.assertEqual(path.read_text(),'preserve')
            child.communicate('\n',timeout=5)
            self.assertFalse(apt.locked([path]))
        finally:
            if child.poll() is None:child.terminate();child.communicate(timeout=5)

    def test_incomplete_update_does_not_succeed_just_because_apt_exits_zero(self):
        bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503 Service Unavailable'
        run=mock.Mock(side_effect=[SimpleNamespace(returncode=0,stdout=bad),SimpleNamespace(returncode=0,stdout='updated')])
        fallback=mock.Mock(return_value=True)
        apt.apt(['update'],busy=lambda:False,run=run,fallback=fallback)
        self.assertEqual(run.call_count,2);fallback.assert_called_once_with(bad)
        self.assertIn('APT::Update::Error-Mode=any',run.call_args.args[0])

    def test_install_mirror_failure_refreshes_indices_before_retrying_install(self):
        bad='E: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/pool/docker.deb 503 Service Unavailable'
        run=mock.Mock(side_effect=[SimpleNamespace(returncode=100,stdout=bad),SimpleNamespace(returncode=0,stdout='updated'),SimpleNamespace(returncode=0,stdout='installed')])
        apt.apt(['install','docker.io'],busy=lambda:False,run=run,fallback=mock.Mock(return_value=True))
        commands=[next(a for a in c.args[0] if a in ('update','install')) for c in run.call_args_list]
        self.assertEqual(commands,['install','update','install'])

    def test_cloud_mirror_fallback_backs_up_sources_and_preserves_other_repositories(self):
        sources=self.root/'apt';(sources/'sources.list.d').mkdir(parents=True)
        original='deb http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu jammy main\ndeb http://security.ubuntu.com/ubuntu jammy-security main\n'
        source=sources/'sources.list';source.write_text(original);source.chmod(0o644)
        third=sources/'sources.list.d/nvidia.list';third.write_text('deb [signed-by=/keys/nvidia.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /\n')
        deb822=sources/'sources.list.d/ubuntu.sources';deb822.write_text('Types: deb\nURIs: http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu\nSuites: jammy-updates\nComponents: main\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        before_third=third.read_bytes();opener=mock.MagicMock();opener.return_value.__enter__.return_value.status=200
        backup=self.root/'backups';bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503'
        self.assertTrue(apt.use_ubuntu_archive(bad,apt_root=sources,backup_root=backup,opener=opener))
        self.assertIn('https://archive.ubuntu.com/ubuntu jammy main',source.read_text())
        self.assertIn('http://security.ubuntu.com/ubuntu',source.read_text());self.assertEqual(third.read_bytes(),before_third)
        self.assertIn('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg',deb822.read_text())
        self.assertEqual(source.stat().st_mode & 0o777,0o644)
        saved=next(backup.glob('*/sources.list'));self.assertEqual(saved.read_text(),original)
        self.assertEqual(saved.stat().st_mode & 0o777,0o600)

    def test_deb822_only_sources_supply_the_correct_ubuntu_suite(self):
        sources=self.root/'apt';(sources/'sources.list.d').mkdir(parents=True)
        (sources/'sources.list.d/ubuntu.sources').write_text('URIs: http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu\nSuites: noble noble-updates\n')
        self.assertEqual(apt.ubuntu_suite(sources),'noble')

    def test_third_party_failure_never_switches_the_ubuntu_mirror(self):
        opener=mock.Mock()
        bad='Hit: http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu jammy\nW: Failed to fetch https://nvidia.github.io/Packages 503'
        self.assertFalse(apt.use_ubuntu_archive(bad,apt_root=self.root,backup_root=self.root/'backups',opener=opener))
        opener.assert_not_called();self.assertFalse((self.root/'backups').exists())

    def test_unreachable_archive_does_not_modify_the_configured_mirror(self):
        sources=self.root/'apt';sources.mkdir();source=sources/'sources.list'
        original='deb http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu jammy main\n';source.write_text(original)
        bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503'
        self.assertFalse(apt.use_ubuntu_archive(bad,apt_root=sources,backup_root=self.root/'backups',opener=mock.Mock(side_effect=OSError('unreachable'))))
        self.assertEqual(source.read_text(),original);self.assertFalse((self.root/'backups').exists())

    def test_custom_mirrors_are_not_rewritten(self):
        sources=self.root/'apt';sources.mkdir();source=sources/'sources.list';source.write_text('deb https://mirror.example.test/ubuntu jammy main\n')
        opener=mock.Mock();bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503'
        self.assertFalse(apt.use_ubuntu_archive(bad,apt_root=sources,backup_root=self.root/'backups',opener=opener))
        opener.assert_not_called();self.assertIn('mirror.example.test',source.read_text())

    def test_fallback_changes_only_the_cloud_host_that_actually_failed(self):
        sources=self.root/'apt';sources.mkdir();source=sources/'sources.list'
        source.write_text('deb http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu jammy main\ndeb http://iad-ad-2.clouds.archive.ubuntu.com/ubuntu jammy-updates main\n')
        opener=mock.MagicMock();opener.return_value.__enter__.return_value.status=200
        bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503'
        self.assertTrue(apt.use_ubuntu_archive(bad,apt_root=sources,backup_root=self.root/'backups',opener=opener))
        self.assertIn('https://archive.ubuntu.com/ubuntu jammy main',source.read_text())
        self.assertIn('http://iad-ad-2.clouds.archive.ubuntu.com/ubuntu jammy-updates main',source.read_text())

    def test_persistent_repository_failure_stops_after_one_fallback(self):
        bad='W: Failed to fetch http://iad-ad-1.clouds.archive.ubuntu.com/ubuntu/dists/jammy/InRelease 503'
        fallback=mock.Mock(return_value=True);run=mock.Mock(return_value=SimpleNamespace(returncode=100,stdout=bad))
        with self.assertRaises(RuntimeError):apt.apt(['update'],busy=lambda:False,run=run,fallback=fallback)
        self.assertEqual(run.call_count,2);self.assertEqual(fallback.call_count,1)

    def test_only_the_known_apt_lock_and_dependent_runcmd_failure_are_eligible(self):
        self.assertTrue(gpu.eligible(self.errors))
        self.assertFalse(gpu.eligible([]))
        self.assertFalse(gpu.eligible([self.errors[1]]))
        self.assertFalse(gpu.eligible(self.errors+["('disk_setup', RuntimeError('failed'))"]))
        self.assertFalse(gpu.eligible(["('package_update_upgrade_install', RuntimeError('repository failed'))"]))

    def test_earlier_stage_errors_block_recovery_classification(self):
        result=self.root/'result.json';status=self.root/'status.json'
        result.write_text(json.dumps({'v1':{'errors':self.errors}}))
        status.write_text(json.dumps({'v1':{'init':{'recoverable_errors':{'ERROR':['another failure']}}}}))
        with mock.patch.object(gpu,'RESULT',result),mock.patch.object(gpu,'STATUS',status):
            with self.assertRaises(RuntimeError):gpu.cloud_errors()

    def test_rendered_repair_script_has_expected_mount_and_model_without_placeholders(self):
        cfg={'model_revision':'a'*40,'vllm_image':'fixture-image'}
        context={'shared':{'fss_mount_ip':{'value':'10.0.1.2'}},'vllm':{'private_ip':{'value':'10.0.1.3'},'instance_id':{'value':'ocid1.instance.fixture'}}}
        script=gpu.render_bootstrap(ROOT,cfg,context)
        self.assertNotIn('${',script);self.assertIn('10.0.1.2:/models',script)
        self.assertIn('/usr/local/sbin/poc-apt install -y docker.io',script)
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        program=gpu.program(ROOT,'repair',cfg,context)
        compile(program,'remote-repair','exec')

    def test_repair_uses_terraform_image_pin_when_customer_config_has_no_image_field(self):
        context={'shared':{'fss_mount_ip':{'value':'10.0.1.2'}},'vllm':{'private_ip':{'value':'10.0.1.3'},'instance_id':{'value':'ocid1.instance.fixture'}}}
        script=gpu.render_bootstrap(ROOT,{'model_revision':'a'*40},context)
        self.assertIn(gpu.configured_image(ROOT,{}),script)
        self.assertIn('@sha256:',gpu.configured_image(ROOT,{}))
        compile(gpu.program(ROOT,'inspect',{'model_revision':'a'*40},context),'remote-inspect','exec')

    def test_repair_refuses_other_failure_before_upload_or_bootstrap(self):
        with mock.patch.object(gpu,'cloud_errors',return_value=['unrelated failure']),mock.patch.object(gpu,'install_payload') as install:
            with self.assertRaises(RuntimeError):gpu.repair({'expected':self.expected})
            install.assert_not_called()

    def test_repeating_verified_repair_checks_health_without_restarting_bootstrap(self):
        marker=self.root/'repair.json';marker.write_text('{}')
        with mock.patch.object(gpu,'MARKER',marker),mock.patch.object(gpu,'verify',return_value={'status':'REPAIRED'}) as verify, \
             mock.patch.object(gpu,'install_payload') as install:
            self.assertEqual(gpu.repair({'expected':self.expected})['status'],'REPAIRED')
            verify.assert_called_once_with(self.expected);install.assert_not_called()

    def test_repair_does_not_write_success_record_when_live_health_fails(self):
        marker=self.root/'repair.json'
        with mock.patch.object(gpu,'cloud_errors',return_value=self.errors),mock.patch.object(gpu,'MARKER',marker), \
             mock.patch.object(gpu,'install_payload'),mock.patch.object(gpu.shutil,'which',return_value='/usr/bin/nvidia-smi'), \
             mock.patch.object(gpu.subprocess,'run',return_value=SimpleNamespace(stdout='failed')), \
             mock.patch.object(gpu,'assert_healthy',side_effect=RuntimeError('not healthy')), \
             mock.patch.object(gpu.time,'monotonic',side_effect=[0,601]):
            with self.assertRaises(RuntimeError):gpu.repair({'expected':self.expected})
        self.assertFalse(marker.exists())

    def test_each_required_service_must_be_active(self):
        def unit(argv,**kwargs):
            if argv[-1]=='nginx':raise subprocess.CalledProcessError(3,argv)
        with mock.patch.object(gpu.subprocess,'run',side_effect=unit),mock.patch.object(gpu,'capture') as capture:
            with self.assertRaises(subprocess.CalledProcessError):gpu.assert_healthy(self.expected)
            capture.assert_not_called()

    def test_repair_proof_checks_identity_errors_source_and_current_health(self):
        marker=self.root/'repair.json';bootstrap=self.root/'bootstrap.sh';bootstrap.write_bytes(b'fixture')
        record=dict(self.expected,status='REPAIRED',original_cloud_init_errors=self.errors);marker.write_text(json.dumps(record))
        info=SimpleNamespace(st_uid=0,st_mode=0o100600)
        with mock.patch.object(gpu,'MARKER',marker),mock.patch.object(gpu,'BOOTSTRAP',bootstrap), \
             mock.patch.object(Path,'lstat',return_value=info),mock.patch.object(Path,'is_symlink',return_value=False), \
             mock.patch.object(gpu,'cloud_errors',return_value=self.errors),mock.patch.object(gpu,'assert_healthy') as health:
            self.assertEqual(gpu.verify(self.expected)['status'],'REPAIRED');health.assert_called_once()
            record['instance_id']='ocid1.instance.other';marker.write_text(json.dumps(record))
            with self.assertRaises(RuntimeError):gpu.verify(self.expected)

    def test_cloud_init_failure_is_never_ignored_without_valid_live_repair_proof(self):
        host=mock.Mock();host.ssh.side_effect=[subprocess.CalledProcessError(1,['ssh']),subprocess.CalledProcessError(1,['ssh'])]
        lab=SimpleNamespace(config={})
        with mock.patch.object(gpu,'program',return_value=b'verify'):
            with self.assertRaises(hosts.PocError):hosts.wait_gpu_bootstrap(host,lab,{},30)

    def test_verified_recovery_allows_normal_health_checks_to_continue(self):
        host=mock.Mock();host.ssh.side_effect=[subprocess.CalledProcessError(1,['ssh']),b'{"status":"REPAIRED"}']
        with mock.patch.object(gpu,'program',return_value=b'verify'):
            hosts.wait_gpu_bootstrap(host,SimpleNamespace(config={}),{},30)
        self.assertEqual(host.ssh.call_count,2)


if __name__=='__main__':unittest.main()
