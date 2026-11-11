#!/usr/bin/env python3
"""Verify live native retrieval, private generation, citations and refusal."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from rag_poc import ROOT, RagPoc, private_json, PocError

CASES=[
    {'question':'What are Project Aster\'s release approval code and rollback verification phrase?',
     'expected':['LANTERN-731','MAPLE-4729'],'source':'01-aster-release-policy.txt'},
    {'question':'Which queue handles Project Aster\'s first escalation, and within how many minutes must a critical incident be acknowledged?',
     'expected':['ORCHID-DESK','17'],'source':'02-aster-support.txt'},
    {'question':'What is Project Aster\'s synthetic data classification and exported evaluation report retention period?',
     'expected':['SYNTHETIC-ASTER','13'],'source':'03-aster-data-handling.txt'},
    {'question':'What is Project Aster\'s synthetic purge review code?',
     'expected':['CEDAR-908'],'source':'03-aster-data-handling.txt'},
    {'question':'What was Project Aster\'s annual revenue for 2027?',
     'expected':['The knowledge base does not contain that information.'],'refusal':True},
]


def reports(value):
    found=[]
    def visit(item):
        if isinstance(item,dict):
            if item.get('stage')=='native_aidp_rag_private_vllm':found.append(item)
            else:
                for child in item.values():visit(child)
        elif isinstance(item,list):
            for child in item:visit(child)
        elif isinstance(item,str):
            try:visit(json.loads(item))
            except (ValueError,RecursionError):pass
    visit(value)
    return found


def check(case,report):
    answer=report.get('answer','')
    chunks=report.get('retrieved_chunks',[])
    supported=case.get('refusal') or all(any(token.lower() in c.get('content','').lower()
        for c in chunks) for token in case['expected'])
    cited=case.get('refusal') or (case['source'] in answer and
        any(c.get('source')==case['source'] for c in chunks))
    return bool(report.get('status')=='PASS' and report.get('private_vllm_used') is True and
        report.get('retrieval_verified') is True and supported and cited and
        all(token.lower() in answer.lower() for token in case['expected']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'poc-config.json')
    args=parser.parse_args()
    poc=RagPoc(args.config)
    results=[]
    for case in CASES:
        response=poc.invoke('__RAG_VERIFY__:'+case['question'],emit=False)
        found=reports(response)
        report=found[0] if len(found)==1 else {'status':'FAIL','reason':'Missing unique report'}
        passed=check(case,report)
        results.append({'question':case['question'],'passed':passed,'report':report})
        print('CASE',len(results),'PASS' if passed else 'FAIL',flush=True)
    output={'status':'PASS' if all(r['passed'] for r in results) else 'FAIL',
        'validated_utc':datetime.now(timezone.utc).isoformat(),'cases':results,
        'profile':poc.cfg['oci_profile'],'region':poc.cfg['region'],
        'aidp_id':poc.api.aidp_id,'knowledge_base':poc.saved['native_knowledge_base']}
    private_json(poc.folder/'rag-acceptance.json',output)
    print('RAG acceptance:',output['status'],str(poc.folder/'rag-acceptance.json'))
    if output['status']!='PASS':raise PocError('One or more live RAG acceptance cases failed.')


if __name__=='__main__':main()
