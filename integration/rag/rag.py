#!/usr/bin/env python3
"""Reusable native AIDP RAG commands, configured by the existing deployment."""
import argparse
from pathlib import Path
import subprocess
import sys

HERE=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','upload-documents','record-kb','connect','verify','status','ask'])
    parser.add_argument('--config',type=Path,default=HERE.parents[1]/'poc-config.json')
    parser.add_argument('--documents-dir',type=Path)
    parser.add_argument('--query')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.action in {'prepare','upload-documents','record-kb','connect'} and not args.apply:
        parser.error('This action requires --apply.')
    def run(script,*extra):
        subprocess.run([sys.executable,str(HERE/script),'--config',str(args.config.resolve()),*extra],check=True)
    if args.action=='upload-documents':
        extra=['--apply']
        if args.documents_dir:extra+=['--documents-dir',str(args.documents_dir.resolve())]
        run('upload_documents.py',*extra)
    elif args.action=='connect':
        run('render_rag.py')
        run('rag_poc.py','upload','--apply')
        run('rag_poc.py','publish','--apply')
        run('rag_poc.py','wait')
        run('verify_rag.py')
    elif args.action=='verify':run('verify_rag.py')
    elif args.action=='ask':
        if not args.query:parser.error('ask requires --query.')
        run('rag_poc.py','invoke','--query',args.query)
    else:run('rag_poc.py',args.action,*(['--apply'] if args.apply else []))

if __name__=='__main__':main()
