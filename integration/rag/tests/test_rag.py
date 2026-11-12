import asyncio
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import runpy
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(HERE),str(HERE.parent/'aidp')]
import rag_agent
from rag_poc import RagPoc, PocError, PendingCreate
from render_rag import render
from verify_rag import check


class RagTests(unittest.TestCase):
    def test_wallet_private_and_removed(self):
        fields=['cwalletsso','ewalletp12','ewalletpem','keystorejks',
                'ojdbcproperties','sqlnetora','tnsnamesora','truststorejks']
        wallet=SimpleNamespace(**{k:base64.b64encode(b'fixture').decode() for k in fields})
        credentials=SimpleNamespace(data=SimpleNamespace(wallet=wallet))
        with rag_agent.secure_wallet(credentials) as folder:
            path=Path(folder)
            self.assertEqual(path.stat().st_mode & 0o777,0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777==0o600 for p in path.iterdir()))
            self.assertIn('SSL_SERVER_DN_MATCH = yes',(path/'sqlnet.ora').read_text())
        self.assertFalse(path.exists())

    def test_source_citations_omit_query_tokens(self):
        self.assertEqual(rag_agent.source_label('https://storage.example/path/policy.txt?token=private#42'),'policy.txt')

    def test_no_empty_retrieval_fallback(self):
        with self.assertRaises(ValueError):rag_agent.grounded_messages('Question',[])

    def test_grounding_and_document_instruction_boundary(self):
        messages=rag_agent.grounded_messages('Question',[{'source':'policy.txt','content':'LANTERN-731'}])
        self.assertIn('untrusted data',messages[0]['content'])
        self.assertIn(rag_agent.NO_INFORMATION,messages[0]['content'])
        self.assertIn('[policy.txt]',messages[1]['content'])

    def test_graph_retrieves_before_private_generation(self):
        chunks=[{'source':'policy.txt','chunk_id':'1','content':'Approval LANTERN-731','score':0.8}]
        client=Mock();client.chat.return_value=({'content':'LANTERN-731 [policy.txt]'}, {'completion_tokens':12})
        client.last_request_metadata={'correlation_id':'fixture-123','http_status':200}
        with patch.object(rag_agent,'EMBEDDED_CONFIG',{'model':'fixture'}), \
             patch.object(rag_agent,'KB_CONFIG',{'knowledgeBase':'fixture'}), \
             patch.object(rag_agent,'retrieve_native',return_value=chunks) as retrieve, \
             patch.object(rag_agent,'PrivateClient',return_value=client):
            agent=rag_agent.AgentBasic();agent.setup()
            result=asyncio.run(agent.invoke('__RAG_REQUEST_ID__:fixture-123\n__RAG_VERIFY__:What is the approval code?'))
        report=json.loads(result['messages'][0]['content'])
        self.assertTrue(report['private_vllm_used'])
        self.assertEqual(report['retrieved_chunks'],chunks)
        self.assertEqual(report['correlation_id'],'fixture-123')
        retrieve.assert_called_once_with('What is the approval code?')
        self.assertIn('LANTERN-731',client.chat.call_args.args[0][1]['content'])
        self.assertEqual(client.chat.call_args.kwargs['correlation_id'],'fixture-123')

    def test_retrieval_failure_does_not_call_model(self):
        with patch.object(rag_agent,'EMBEDDED_CONFIG',{'model':'fixture'}), \
             patch.object(rag_agent,'KB_CONFIG',{'knowledgeBase':'fixture'}), \
             patch.object(rag_agent,'retrieve_native',side_effect=RuntimeError('fixture')), \
             patch.object(rag_agent,'PrivateClient') as client:
            agent=rag_agent.AgentBasic();agent.setup()
            result=asyncio.run(agent.invoke('__RAG_VERIFY__:Question'))
        self.assertEqual(json.loads(result['messages'][0]['content'])['status'],'FAIL')
        client.assert_not_called()

    def test_render_rejects_secret_config(self):
        with self.assertRaises(ValueError):render({'api_key':'forbidden'},{})

    def test_acceptance_rejects_unsupported_model_guess(self):
        case={'expected':['LANTERN-731'],'source':'policy.txt'}
        report={'status':'PASS','private_vllm_used':True,'retrieval_verified':True,
                'answer':'LANTERN-731 [policy.txt]',
                'retrieved_chunks':[{'source':'policy.txt','content':'Unrelated source text.'}]}
        self.assertFalse(check(case,report))

    def test_acceptance_requires_source_citation(self):
        case={'expected':['LANTERN-731'],'source':'policy.txt'}
        report={'status':'PASS','private_vllm_used':True,'retrieval_verified':True,
                'answer':'LANTERN-731',
                'retrieved_chunks':[{'source':'policy.txt','content':'LANTERN-731'}]}
        self.assertFalse(check(case,report))

    def test_create_reconciles_keyless_success_but_rejects_existing_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            poc=object.__new__(RagPoc)
            poc.saved={'resources':{},'tokens':{}}
            poc.inventory_path=Path(tmp)/'inventory.json'
            poc.api=Mock()
            poc.api.items.side_effect=[[],[{'key':'default.fixture','displayName':'fixture'}]]
            poc.api.request.return_value=({}, {'opc-request-id':'fixture'})
            item=poc.create('schema','/schemas',{'displayName':'fixture'},'/schemas?catalogKey=default')
            self.assertEqual(item['key'],'default.fixture')
            self.assertIn('schema',poc.saved['create_intents'])
            poc.api.items.side_effect=None
            poc.api.items.return_value=[{'key':'unowned','displayName':'collision'}]
            with self.assertRaises(PocError):
                poc.create('volume','/volumes',{'displayName':'collision'},'/volumes')
            self.assertEqual(poc.api.request.call_count,1)

    def test_retry_adopts_only_accepted_matching_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            poc=object.__new__(RagPoc)
            poc.inventory_path=Path(tmp)/'inventory.json'
            body={'displayName':'owned'}
            poc.saved={'resources':{},'tokens':{},
                'create_intents':{'schema':{'path':'/schemas','body':body}},
                'create_responses':{'schema':{'data':{},'headers':{}}}}
            poc.api=Mock()
            poc.api.items.return_value=[{'key':'default.owned','displayName':'owned'}]
            item=poc.create('schema','/schemas',body,'/schemas')
            self.assertEqual(item['key'],'default.owned')
            poc.api.request.assert_not_called()
            poc.saved['resources']={}
            poc.api.items.return_value=[]
            with self.assertRaisesRegex(PocError,'still pending'):
                poc.create('schema','/schemas',body,'/schemas')
            poc.api.request.assert_not_called()

    def test_accepted_async_prepare_is_resumable_without_duplicate_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            poc=object.__new__(RagPoc);poc.schema='fixture_rag'
            poc.inventory_path=Path(tmp)/'inventory.json';poc.saved={'resources':{},'tokens':{}}
            poc.api=Mock();poc.api.items.return_value=[]
            poc.api.request.return_value=({}, {'aidp-async-operation-key':'operation-fixture'})
            poc.api.get.return_value={'status':'IN_PROGRESS','resourceName':'default.fixture_rag'}
            output=io.StringIO()
            with contextlib.redirect_stdout(output):poc.prepare()
            self.assertIn('still pending discovery',output.getvalue())
            self.assertIn('IN_PROGRESS',output.getvalue())
            self.assertEqual(poc.api.request.call_count,1)
            token=poc.saved['tokens']['schema']
            with contextlib.redirect_stdout(io.StringIO()):poc.prepare()
            self.assertEqual(poc.api.request.call_count,1)
            self.assertEqual(poc.saved['tokens']['schema'],token)
            poc.api.items.return_value=[{'key':'default.fixture_rag','displayName':'fixture_rag','lifecycleState':'ACTIVE'}]
            body=poc.saved['create_intents']['schema']['body']
            item=poc.create('schema','/schemas',body,'/schemas?catalogKey=default')
            self.assertEqual(item['lifecycleState'],'ACTIVE')
            self.assertEqual(poc.saved['resources']['schema']['async_operation_key'],'operation-fixture')
            self.assertEqual(poc.api.request.call_count,1)

    def test_pending_status_reports_async_operation_without_writing_inventory(self):
        poc=object.__new__(RagPoc);poc.api=Mock();poc.save=Mock()
        poc.saved={'resources':{},'create_intents':{'schema':{'body':{'displayName':'fixture'}}},
                   'create_responses':{'schema':{'headers':{'aidp-async-operation-key':'fixture-op'}}}}
        poc.api.get.return_value={'status':'SUCCEEDED','resourceName':'default.fixture','errorCode':''}
        output=io.StringIO()
        with contextlib.redirect_stdout(output):poc.status()
        self.assertIn('SUCCEEDED',output.getvalue());self.assertIn('fixture-op',output.getvalue())
        poc.api.get.assert_called_once_with('/asyncOperations/fixture-op')
        poc.api.request.assert_not_called();poc.save.assert_not_called()
        self.assertEqual(poc.saved['resources'],{})

    def test_failed_async_prepare_remains_an_error_without_replacement_create(self):
        for status in ('FAILED','CANCELED'):
            poc=object.__new__(RagPoc);poc.schema='fixture';poc.api=Mock()
            body={'displayName':'fixture','catalogName':'default',
                  'description':'Synthetic vLLM/AIDP RAG POC; owned by integration/rag'}
            poc.saved={'resources':{},'create_intents':{'schema':{'path':'/schemas','body':body}},
                       'create_responses':{'schema':{'headers':{'aidp-async-operation-key':'fixture-op'}}}}
            poc.api.items.return_value=[];poc.api.get.return_value={'status':status}
            with self.subTest(status=status),self.assertRaisesRegex(PocError,status):poc.prepare()
            poc.api.request.assert_not_called()

    def test_changed_accepted_intent_is_rejected_even_before_resource_visibility(self):
        poc=object.__new__(RagPoc);poc.api=Mock();poc.api.items.return_value=[]
        poc.saved={'resources':{},'create_intents':{'schema':{'path':'/schemas','body':{'displayName':'original'}}},
                   'create_responses':{'schema':{'headers':{}}}}
        with self.assertRaisesRegex(PocError,'intent differs'):
            poc.create('schema','/schemas',{'displayName':'changed'},'/schemas')
        poc.api.request.assert_not_called()

    def test_empty_and_unconfirmed_status_are_not_silent(self):
        poc=object.__new__(RagPoc);poc.api=Mock();poc.saved={'resources':{}}
        output=io.StringIO()
        with contextlib.redirect_stdout(output):poc.status()
        self.assertIn('No RAG resources',output.getvalue())
        poc.saved['create_intents']={'schema':{'body':{'displayName':'unconfirmed'}}}
        output=io.StringIO()
        with contextlib.redirect_stdout(output):poc.status()
        self.assertIn('UNCONFIRMED',output.getvalue());poc.api.request.assert_not_called()

    def test_ambiguous_post_discovery_remains_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            poc=object.__new__(RagPoc);poc.api=Mock();poc.saved={'resources':{},'tokens':{}}
            poc.inventory_path=Path(tmp)/'inventory.json'
            poc.api.items.side_effect=[[],[{'key':'one','displayName':'fixture'},{'key':'two','displayName':'fixture'}]]
            poc.api.request.return_value=({}, {})
            with self.assertRaisesRegex(PocError,'multiple resources'):
                poc.create('schema','/schemas',{'displayName':'fixture'},'/schemas')
            self.assertEqual(poc.saved['resources'],{})

    def test_rag_wrapper_preserves_child_error_without_extra_traceback(self):
        with patch.object(sys,'argv',['rag.py','status','--config','fixture.json']), \
             patch('subprocess.run',side_effect=subprocess.CalledProcessError(7,['child'])):
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(HERE/'rag.py'),run_name='__main__')
            self.assertEqual(error.exception.code,7)

    def test_cli_correlation_metadata_and_opt_in_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            poc=object.__new__(RagPoc)
            poc.folder=Path(tmp)
            poc.saved={'resources':{'deployment':{'resource_path':'/owned/deployment'}}}
            poc.api=Mock()
            poc.api.get.return_value={'lifecycleState':'ACTIVE','endpointUrl':'https://gateway.aidp.example'}
            poc.api.request.return_value=({'id':'response-fixture'},{'opc-request-id':'gateway-fixture'})
            poc.invoke('SYNTHETIC QUESTION',correlation_id='fixture-123',trace=True,emit=False)
            body=poc.api.request.call_args.args[2]
            self.assertTrue(body['trace'])
            self.assertEqual(body['input'][0]['content'][0]['text'],
                '__RAG_REQUEST_ID__:fixture-123\nSYNTHETIC QUESTION')
            self.assertEqual(poc.api.request.call_args.kwargs['headers']['opc-request-id'],'fixture-123')
            record=(poc.folder/'requests/fixture-123.json').read_text()
            self.assertNotIn('SYNTHETIC QUESTION',record)
            with self.assertRaises(PocError):
                poc.invoke('Another question',correlation_id='fixture-123',emit=False)
            self.assertEqual(poc.api.request.call_count,1)


if __name__=='__main__':unittest.main()
