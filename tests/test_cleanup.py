"""Cleanup orchestration and dependency ordering using an in-memory AIDP API."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import cleanup
import poc
from aidp_workspace import ApiError


class FakeApi:
    aidp_id = 'ocid1.aidataplatform.oc1.iad.fixture'

    def __init__(self):
        self.resources = {
            '/workspaces/ws': {'key':'ws','displayName':'fixture-poc-private','type':'USER'},
            '/workspaces/default': {'key':'default','displayName':'fixture-poc-workspace','type':'DEFAULT'},
            '/workspaces/ws/clusters/cpu': {'key':'cpu','displayName':'fixture_poc_ai_compute','type':'AI_COMPUTE','state':'ACTIVE'},
            '/workspaces/ws/agents/base': {'key':'base','displayName':'fixture_poc_agent','type':'CODE','computeKey':'cpu',
                                           'deploymentState':{'TEST':'ACTIVE'}},
            '/workspaces/ws/agents/rag': {'key':'rag','displayName':'fixture_poc_rag_agent','type':'CODE','computeKey':'cpu'},
            '/workspaces/ws/agents/base/deployments/prod': {'key':'prod','displayName':'base-endpoint','agentKey':'base'},
            '/workspaces/ws/agents/base/deployments/old': {'key':'old','displayName':'previous-version','agentKey':'base'},
            '/workspaces/ws/agents/rag/deployments/rag-prod': {'key':'rag-prod','displayName':'rag-endpoint','agentKey':'rag'},
            '/schemas/default.fixture_poc_rag': {'key':'default.fixture_poc_rag','displayName':'fixture_poc_rag','catalogName':'default'},
            '/volumes/default.fixture_poc_rag.synthetic_documents': {'key':'default.fixture_poc_rag.synthetic_documents',
                'displayName':'synthetic_documents','schemaName':'fixture_poc_rag','volumeType':'MANAGED'},
            '/credentials/credential': {'key':'credential','displayName':'fixture_poc_vllm_key'},
            '/credentials/probe': {'key':'probe','displayName':'fixture_poc_credential_probe'},
        }
        self.files = [{'path':'01.txt','displayName':'01.txt','type':'FILE'}]
        self.calls, self.async_states = [], {}
        self.async_delete = False
        self.interrupt_after_delete = None

    def get(self, path):
        return self.request('GET', path)[0]

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        url = urlsplit(path)
        path, query = url.path, parse_qs(url.query)
        if method == 'GET':
            if path.startswith('/asyncOperations/'):
                return {'status':self.async_states[path.rsplit('/',1)[1]]}, {}
            if path.endswith('/files'):
                return {'items':copy.deepcopy(self.files)}, {}
            if path in self.resources:
                return copy.deepcopy(self.resources[path]), {'etag':'version-1'}
            if path in ['/workspaces','/schemas','/volumes','/credentials'] or path.endswith(('/agents','/clusters','/deployments','/jobs')):
                prefix = path+'/'
                items = [copy.deepcopy(v) for p,v in self.resources.items()
                         if p.startswith(prefix) and '/' not in p[len(prefix):]]
                if 'type' in query:
                    items = [v for v in items if v.get('type') == query['type'][0]]
                return {'items':items}, {}
            raise ApiError(404,'NotFound','fixture-request')
        if method == 'PUT':
            assert '/agents/' in path and body == {'computeKey':None}
            self.resources[path]['computeKey'] = None
            self.resources[path]['deploymentState'] = {'TEST':'INACTIVE'}
            return copy.deepcopy(self.resources[path]), {}
        if method == 'DELETE':
            headers = kwargs.get('headers', {})
            if path.endswith('/actions/deleteFile'):
                self.files = [v for v in self.files if '/'+v['path'] != headers['path']]
            else:
                if '/agents/' in path and '/deployments/' not in path:
                    assert not self.resources[path].get('computeKey'), 'TEST compute was not detached'
                    assert not any(p.startswith(path+'/deployments/') for p in self.resources), 'PROD endpoint still exists'
                if path.startswith('/schemas/'):
                    assert headers['is-forced'] == 'true'
                    self.resources.pop('/volumes/default.fixture_poc_rag.synthetic_documents', None)
                    self.files = []
                if path == '/workspaces/ws':
                    assert not any(p.startswith(path+'/agents/') or p.startswith(path+'/clusters/') for p in self.resources)
                self.resources.pop(path, None)
            if path == self.interrupt_after_delete:
                raise TimeoutError('Lost response after server accepted deletion')
            if self.async_delete:
                token = 'operation-'+str(len(self.async_states))
                self.async_states[token] = 'SUCCEEDED'
                return {}, {'aidp-async-operation-key':token,'opc-request-id':'fixture-request'}
            return {}, {'opc-request-id':'fixture-request'}
        raise AssertionError('Unexpected API operation '+method+' '+path)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cfg = dict(name='fixture-poc',tenancy_id='tenancy',compartment_id='compartment',region='us-ashburn-1',
                        state_directory=str(self.folder/'states'))
        self.baseline = {'aidp_id':FakeApi.aidp_id,'resources':{
            'workspace':{'key':'ws','name':'fixture-poc-private'},
            'compute':{'key':'cpu','name':'fixture_poc_ai_compute'},
            'agent':{'key':'base','name':'fixture_poc_agent'},
            'deployment':{'key':'prod','name':'base-endpoint'}},
            'vault_credential':{'key':'credential','name':'fixture_poc_vllm_key'},
            'synthetic_credential':{'key':'probe','name':'fixture_poc_credential_probe'}}
        self.rag = {'aidp_id':FakeApi.aidp_id,'resources':{
            'schema':{'key':'default.fixture_poc_rag','name':'fixture_poc_rag'},
            'volume':{'key':'default.fixture_poc_rag.synthetic_documents','name':'synthetic_documents'},
            'agent':{'key':'rag','name':'fixture_poc_rag_agent'}},
            'uploaded_documents':{'01.txt':'fixture-hash'},
            'native_knowledge_base':{'catalog':'default','schema':'fixture_poc_rag','name':'aster_knowledge_base'}}
        self.write(self.folder/'aidp-workspace.json', self.baseline)
        self.write(self.folder/'rag/inventory.json', self.rag)
        self.output = mock.patch.object(cleanup,'output',side_effect=lambda cfg,stage:
            {'aidp_id':FakeApi.aidp_id} if stage=='aidp' else {'vault_id':'vault'})
        self.output.start();self.addCleanup(self.output.stop)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)
        self.api = FakeApi()

    def write(self, path, value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value))

    def controller(self, **kwargs):
        return cleanup.RuntimeCleanup(self.cfg,api=self.api,wait_seconds=0,**kwargs)

    def secret(self):
        self.write(self.folder/'runtime/inference-secret.json',
                   {'id':'ocid1.vaultsecret.oc1.iad.fixture','name':'fixture-poc-inference-api-key','vault_id':'vault'})
        return {'id':'ocid1.vaultsecret.oc1.iad.fixture','secret-name':'fixture-poc-inference-api-key',
                'compartment-id':'compartment','vault-id':'vault','lifecycle-state':'ACTIVE'}

    def test_preview_is_get_only_and_includes_all_deployments_and_schema_scope(self):
        report = self.controller().preview()
        self.assertFalse(report['blockers'])
        self.assertEqual({o['key'] for o in report['operations'] if o['kind']=='deployment'}, {'prod','old','rag-prod'})
        self.assertTrue(next(o for o in report['operations'] if o['kind']=='schema')['cascade'])
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_dependency_order_and_verified_empty_runtime(self):
        controller = self.controller();controller.preview();controller.execute()
        calls = [(m,p) for m,p,_,_ in self.api.calls if m!='GET']
        self.assertLess(calls.index(('DELETE','/workspaces/ws/agents/base/deployments/old')),
                        calls.index(('PUT','/workspaces/ws/agents/base')))
        self.assertLess(calls.index(('DELETE','/workspaces/ws/agents/rag')),
                        calls.index(('DELETE','/schemas/default.fixture_poc_rag')))
        self.assertLess(calls.index(('DELETE','/schemas/default.fixture_poc_rag')),
                        calls.index(('DELETE','/workspaces/ws/clusters/cpu')))
        self.assertEqual(set(self.api.resources), {'/workspaces/default'})
        self.assertTrue(controller.saved['runtime_verified'])

    def test_resume_after_lost_delete_response_does_not_repeat_completed_deletion(self):
        controller = self.controller();controller.preview()
        self.api.interrupt_after_delete = '/workspaces/ws/agents/rag/deployments/rag-prod'
        with self.assertRaises(TimeoutError):controller.execute()
        self.api.interrupt_after_delete = None
        controller = self.controller();controller.preview();controller.execute()
        requests = [p for m,p,_,_ in self.api.calls if m=='DELETE']
        self.assertEqual(requests.count('/workspaces/ws/agents/rag/deployments/rag-prod'), 1)
        self.assertTrue(controller.saved['runtime_verified'])

    def test_async_delete_completion_is_waited_and_persisted(self):
        self.api.async_delete = True
        controller = self.controller();controller.preview();controller.execute()
        self.assertTrue(any(p.startswith('/asyncOperations/') for m,p,_,_ in self.api.calls if m=='GET'))
        self.assertTrue(all(r['status']=='DONE' for r in controller.saved['operations'].values()))

    def test_failed_async_operation_blocks_even_when_entity_has_disappeared(self):
        controller = self.controller()
        controller.saved.update(target=controller.target,aidp_id=FakeApi.aidp_id,
            operations={'removed':{'status':'REQUESTED','async_operation_key':'failed'}})
        controller.save();self.api.async_states['failed']='FAILED'
        controller = self.controller();report=controller.preview()
        self.assertTrue(report['blockers'])
        with self.assertRaises(poc.PocError):controller.execute()
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_unrecorded_workspace_blocks_until_explicitly_selected_by_key(self):
        self.api.resources['/workspaces/extra']={'key':'extra','displayName':'Extra workspace','type':'USER'}
        controller=self.controller();self.assertTrue(controller.preview()['blockers'])
        with self.assertRaises(poc.PocError):controller.execute()
        controller=self.controller(additional_workspaces=['extra']);self.assertFalse(controller.preview()['blockers'])
        controller.execute();self.assertNotIn('/workspaces/extra',self.api.resources)

    def test_unknown_resources_block_before_mutation(self):
        cases=[('/workspaces/ws/agents/foreign',{'key':'foreign','displayName':'Foreign agent','type':'CODE'}),
               ('/workspaces/ws/clusters/foreign',{'key':'foreign','type':'USER'}),
               ('/credentials/foreign',{'key':'foreign','displayName':'Other credential'}),
               ('/workspaces/ws/jobs/foreign',{'key':'foreign','displayName':'Other job'})]
        for path,item in cases:
            with self.subTest(path=path):
                self.api=FakeApi();self.api.resources[path]=item
                controller=self.controller();self.assertTrue(controller.preview()['blockers'])
                with self.assertRaises(poc.PocError):controller.execute()
                self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_unrecorded_documents_and_external_volumes_are_not_deleted(self):
        self.api.files.append({'path':'other.txt','type':'FILE'})
        controller=self.controller();self.assertTrue(controller.preview()['blockers'])
        with self.assertRaises(poc.PocError):controller.execute()
        self.api.resources['/volumes/default.fixture_poc_rag.synthetic_documents']['volumeType']='EXTERNAL'
        with self.assertRaises(poc.PocError):self.controller().preview()

    def test_foreign_schema_path_or_aidp_inventory_is_rejected(self):
        self.rag['resources']['schema']['key']='default.someone_else'
        self.write(self.folder/'rag/inventory.json',self.rag)
        with self.assertRaises(poc.PocError):self.controller().preview()
        self.rag['aidp_id']='foreign'
        self.write(self.folder/'rag/inventory.json',self.rag)
        with self.assertRaises(poc.PocError):self.controller()

    def test_pending_create_intent_and_missing_baseline_block_cleanup(self):
        self.rag['create_intents']={'volume_pending':{'path':'/volumes'}}
        self.write(self.folder/'rag/inventory.json',self.rag)
        with self.assertRaises(poc.PocError):self.controller()
        (self.folder/'aidp-workspace.json').unlink()
        with self.assertRaises(poc.PocError):self.controller()

    def test_404_requires_authoritative_listing_and_403_is_never_missing(self):
        controller=self.controller()
        with mock.patch.object(self.api,'request',side_effect=ApiError(403,'NotAuthorized','id')):
            with self.assertRaises(ApiError):controller.preview()
        original=self.api.request
        def hide(method,path,*args,**kwargs):
            if path=='/workspaces/ws/agents/base':raise ApiError(404,'NotFound','id')
            return original(method,path,*args,**kwargs)
        with mock.patch.object(self.api,'request',side_effect=hide):
            with self.assertRaises(poc.PocError):controller.preview()

    def test_malformed_listing_does_not_certify_absence(self):
        with mock.patch.object(self.api,'request',return_value=({},{})):
            with self.assertRaises(poc.PocError):self.controller().preview()

    def test_secret_preview_does_not_schedule_and_apply_requires_runtime_verification(self):
        secret=self.secret();controller=self.controller()
        with mock.patch.object(cleanup,'oci',return_value=secret) as cloud:
            cleanup.schedule_secret(self.cfg,controller)
            self.assertEqual(cloud.call_count,1)
            with self.assertRaises(poc.PocError):cleanup.schedule_secret(self.cfg,controller,apply=True)
        controller.saved['runtime_verified']=True
        pending=dict(secret,**{'lifecycle-state':'PENDING_DELETION','time-of-deletion':'deadline'})
        with mock.patch.object(cleanup,'oci',side_effect=[secret,{},pending]) as cloud:
            result=cleanup.schedule_secret(self.cfg,controller,apply=True)
            self.assertEqual(cloud.call_args_list[1].args[1:4],('vault','secret','schedule-secret-deletion'))
            self.assertEqual(result['state'],'PENDING_DELETION')

    def test_pending_secret_deadline_is_preserved_and_foreign_vault_blocks(self):
        secret=self.secret();controller=self.controller();controller.saved['runtime_verified']=True
        secret.update({'lifecycle-state':'PENDING_DELETION','time-of-deletion':'original-deadline'})
        with mock.patch.object(cleanup,'oci',return_value=secret) as cloud:
            result=cleanup.schedule_secret(self.cfg,controller,apply=True)
            self.assertEqual(result['time_of_deletion'],'original-deadline');self.assertEqual(cloud.call_count,1)
        secret['vault-id']='other'
        with mock.patch.object(cleanup,'oci',return_value=secret):
            with self.assertRaises(poc.PocError):cleanup.schedule_secret(self.cfg,controller,apply=True)

    def test_secret_create_intent_without_inventory_blocks(self):
        self.write(self.folder/'runtime/secret-create-intent.json',{'secretName':'fixture'})
        with self.assertRaises(poc.PocError):cleanup.schedule_secret(self.cfg,self.controller())

    def test_backup_is_verified_private_and_excludes_provider_cache(self):
        source=self.folder/'source';source.mkdir();(source/'README.md').write_text('source')
        self.write(source/'MANIFEST.json',{'sha256':{'README.md':'fixture'}})
        config=self.folder/'config.json';config.write_text('{}')
        (self.folder/'states').mkdir();(self.folder/'states'/'terraform.tfstate').write_text('state')
        provider=self.folder/'states'/'.terraform';provider.mkdir();(provider/'binary').write_text('excluded')
        record=cleanup.backup_local(self.cfg,config,root=source)
        archive=Path(record['archive']);self.assertEqual(archive.stat().st_mode & 0o777,0o600)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),record['sha256'])
        with tarfile.open(archive) as tar:
            self.assertIn('deployment/states/terraform.tfstate',tar.getnames())
            self.assertIn('source/README.md',tar.getnames())
            self.assertFalse(any('.terraform/' in n for n in tar.getnames()))
        self.assertFalse(record['remote_data_exported'])

    def test_automatic_pipeline_preview_and_backup_failure_never_apply(self):
        args=argparse.Namespace(additional_workspace=[],cleanup_wait_minutes=1,apply=False,yes=True,config=Path('config.json'),
                                delete_data=True,confirm_name='fixture-poc')
        controller=self.controller();preview,apply=mock.Mock(),mock.Mock()
        with mock.patch.object(cleanup,'RuntimeCleanup',return_value=controller),mock.patch.object(cleanup,'schedule_secret'),mock.patch.object(cleanup,'backup_local') as backup:
            cleanup.automated_destroy(self.cfg,args,preview_infrastructure=preview,apply_infrastructure=apply)
            backup.assert_not_called();apply.assert_not_called();preview.assert_called_once()
        args.apply=True
        with mock.patch.object(cleanup,'RuntimeCleanup',return_value=controller),mock.patch.object(cleanup,'schedule_secret'),mock.patch.object(cleanup,'backup_local',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):cleanup.automated_destroy(self.cfg,args,preview_infrastructure=preview,apply_infrastructure=apply)
            apply.assert_not_called()
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_runtime_failure_prevents_secret_mutation_and_infrastructure_apply(self):
        args=argparse.Namespace(additional_workspace=[],cleanup_wait_minutes=1,apply=True,yes=True,config=Path('config.json'),
                                delete_data=True,confirm_name='fixture-poc')
        controller=self.controller();apply=mock.Mock()
        with mock.patch.object(cleanup,'RuntimeCleanup',return_value=controller),mock.patch.object(cleanup,'schedule_secret') as secret, \
             mock.patch.object(cleanup,'backup_local',return_value={'archive':'fixture'}), \
             mock.patch.object(controller,'execute',side_effect=poc.PocError('runtime still active')):
            with self.assertRaises(poc.PocError):cleanup.automated_destroy(self.cfg,args,preview_infrastructure=mock.Mock(),apply_infrastructure=apply)
            self.assertEqual(secret.call_count,1);self.assertFalse(secret.call_args.kwargs.get('apply',False))
            apply.assert_not_called()

    def test_cli_guards_reject_invalid_cleanup_modes_before_loading_config(self):
        commands=[['destroy','--include-runtime','--apply'],
                  ['destroy','--additional-workspace','extra'],
                  ['deploy','--include-runtime'],
                  ['destroy','--include-runtime','--confirm-aidp-runtime-removed'],
                  ['destroy','--cleanup-wait-minutes','0']]
        with mock.patch.object(poc,'read_config') as read, contextlib.redirect_stderr(io.StringIO()):
            for command in commands:
                with self.subTest(command=command),self.assertRaises(SystemExit):poc.main(command)
            read.assert_not_called()

    def test_successful_pipeline_backs_up_before_mutation_and_schedules_secret_last(self):
        secret=self.secret();controller=self.controller();events=[]
        args=argparse.Namespace(additional_workspace=[],cleanup_wait_minutes=1,apply=True,yes=True,config=Path('config.json'),
                                delete_data=True,confirm_name='fixture-poc')
        original=self.api.request
        def request(method,*args,**kwargs):
            if method!='GET':events.append('runtime')
            return original(method,*args,**kwargs)
        def cloud(cfg,*args):
            if args[2]=='schedule-secret-deletion':
                self.assertTrue(controller.saved['runtime_verified'])
                events.append('secret');secret['lifecycle-state']='PENDING_DELETION';return {}
            return dict(secret)
        def backup(*args):events.append('backup');return {'archive':'fixture'}
        with mock.patch.object(cleanup,'RuntimeCleanup',return_value=controller),mock.patch.object(cleanup,'oci',side_effect=cloud), \
             mock.patch.object(cleanup,'backup_local',side_effect=backup),mock.patch.object(self.api,'request',side_effect=request):
            cleanup.automated_destroy(self.cfg,args,preview_infrastructure=lambda:events.append('plan'),
                                      apply_infrastructure=lambda:events.append('infrastructure'))
        self.assertLess(events.index('plan'),events.index('backup'))
        self.assertLess(events.index('backup'),events.index('runtime'))
        self.assertLess(max(i for i,v in enumerate(events) if v=='runtime'),events.index('secret'))
        self.assertLess(events.index('secret'),events.index('infrastructure'))
        self.assertTrue(controller.saved['infrastructure_completed'])

    def test_pending_async_success_is_reconciled_after_resource_disappears(self):
        self.api.resources.pop('/workspaces/ws/agents/base/deployments/old')
        controller=self.controller();controller.saved.update(target=controller.target,aidp_id=FakeApi.aidp_id,
            operations={'/workspaces/ws/agents/base/deployments/old':
                        {'status':'REQUESTED','async_operation_key':'previous'}})
        controller.save();self.api.async_states['previous']='SUCCEEDED'
        controller=self.controller();controller.preview();controller.execute()
        self.assertTrue(controller.saved['runtime_verified'])
        self.assertEqual(controller.saved['operations']['/workspaces/ws/agents/base/deployments/old']['status'],'DONE')
        self.assertFalse(any(m=='DELETE' and p.endswith('/old') for m,p,_,_ in self.api.calls))

    def test_confirmation_cancellation_prevents_backup_and_all_mutations(self):
        args=argparse.Namespace(additional_workspace=[],cleanup_wait_minutes=1,apply=True,yes=False,config=Path('config.json'),
                                delete_data=True,confirm_name='fixture-poc')
        controller=self.controller()
        with mock.patch.object(cleanup,'RuntimeCleanup',return_value=controller),mock.patch.object(cleanup,'schedule_secret'), \
             mock.patch.object(cleanup,'backup_local') as backup,mock.patch('builtins.input',return_value='cancel'):
            with self.assertRaises(poc.PocError):
                cleanup.automated_destroy(self.cfg,args,preview_infrastructure=mock.Mock(),apply_infrastructure=mock.Mock())
            backup.assert_not_called()
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_noninteractive_apply_does_not_pass_yes_to_its_preview_subprocess(self):
        args=argparse.Namespace(yes=True,infra_only=False)
        with mock.patch.object(poc.subprocess,'run') as run:
            poc.inference(self.cfg,apply=False,destroy=True,args=args)
            self.assertNotIn('--yes',run.call_args.args[0])
            self.assertNotIn('--apply',run.call_args.args[0])
            poc.inference(self.cfg,apply=True,destroy=True,args=args)
            self.assertIn('--yes',run.call_args.args[0])
            self.assertIn('--apply',run.call_args.args[0])
            self.assertIn('--delete-data',run.call_args.args[0])

    def test_destroy_cli_integrates_automatic_preview_and_apply_without_manual_ack(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.api=FakeApi()
                argv=['destroy','--include-runtime']
                if apply:argv+=['--apply','--delete-data','--confirm-name','fixture-poc','--yes']
                else:argv+=['--plan']
                with mock.patch.object(poc,'read_config',return_value=self.cfg),mock.patch.object(poc,'target_check'), \
                     mock.patch.object(poc,'inference') as inference,mock.patch.object(cleanup,'Api',return_value=self.api), \
                     mock.patch.object(cleanup,'backup_local',return_value={'archive':'fixture'}) as backup:
                    poc.main(argv)
                    self.assertEqual([c.kwargs['apply'] for c in inference.call_args_list], [False,True] if apply else [False])
                    if apply:backup.assert_called_once()
                    else:
                        backup.assert_not_called()
                        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))


if __name__=='__main__':
    unittest.main()
