#!/usr/bin/env python3
"""Combine tested private transport with the native retrieval entry; no secret values."""
import argparse
import ast
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE.parent/'aidp'))
from render_agent import render as validate_private_config


def render(config,kb):
    validate_private_config(config)
    if set(kb)!={'catalog','schema','knowledgeBase','top_k'} or not 1<=kb['top_k']<=10:
        raise ValueError('Use the declared KB identity and top_k 1–10.')
    client=ast.parse((HERE/'private_transport.py').read_text())
    agent=ast.parse((HERE/'rag_agent.py').read_text())
    nodes=[n for n in client.body if not isinstance(n,ast.If) and
        not (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='main')]
    for node in agent.body:
        if isinstance(node,ast.ImportFrom) and node.module=='private_transport':continue
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in {'EMBEDDED_CONFIG','KB_CONFIG'}:
                    node.value=ast.parse(repr(config if target.id=='EMBEDDED_CONFIG' else kb),mode='eval').body
        nodes.append(node)
    source=ast.unparse(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])))+'\n'
    compile(source,'rag_agent.generated.py','exec')
    return source


def main():
    from rag_poc import RagPoc
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'poc-config.json')
    args=parser.parse_args()
    poc=RagPoc(args.config)
    folder=poc.folder.parent
    baseline=json.loads((folder/'aidp-workspace.json').read_text())
    runtime=json.loads((folder/'runtime/runtime.json').read_text())
    config={'base_url':'https://'+baseline['private_dns_name']+'/v1',
        'private_dns_hostname':baseline['private_dns_name'],'model':runtime['model'],
        'region':runtime['region'],'ca_pem':(folder/'runtime/inference-ca.crt').read_text(),
        'api_key_credential_name':baseline['vault_credential']['name'],
        'api_key_credential_secret_id':baseline['vault_credential']['secret_id'],
        'timeout_seconds':120,'max_tokens':512}
    kb=json.loads((folder/'rag/inventory.json').read_text())['native_knowledge_base']
    params={'catalog':kb['catalog'],'schema':kb['schema'],'knowledgeBase':kb['name'],'top_k':3}
    output=folder/'rag/rag_agent.generated.py'
    output.write_text(render(config,params));output.chmod(0o600)
    print('Rendered non-secret native RAG / private vLLM agent:',output)


if __name__=='__main__':main()
