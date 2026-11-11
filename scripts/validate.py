#!/usr/bin/env python3
"""Local checks only. Terraform tests use mock providers; no OCI requests/applies."""
import argparse
import os
from pathlib import Path
import py_compile
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def run(*argv):
    subprocess.run([str(a) for a in argv],cwd=ROOT,check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plugin-dir',type=Path,help='Optional trusted offline provider mirror')
    p.add_argument('--app-python',type=Path,help='Interpreter with apps/streamlit requirements installed')
    p.add_argument('--agent-python',type=Path,help='Interpreter with LangGraph 1.0.1 installed')
    args=p.parse_args()
    for file in ROOT.rglob('*.py'):
        if not any(v in file.parts for v in ('.terraform','.venv','deployments','.runtime')):
            py_compile.compile(str(file),doraise=True)
    for folder in (ROOT,ROOT/'runtime',ROOT/'apps/streamlit',ROOT/'scripts'):
        for file in folder.rglob('*.sh'):
            if not any(v in file.parts for v in ('.venv','.terraform','deployments','.runtime')):
                run('bash','-n',file)
    run('terraform','fmt','-check','-recursive','terraform')
    for folder in sorted((ROOT/'terraform').iterdir()):
        command=['terraform',f'-chdir={folder}','init','-backend=false','-input=false','-no-color']
        if args.plugin_dir:command+=['-plugin-dir='+str(args.plugin_dir.resolve())]
        run(*command)
        run('terraform',f'-chdir={folder}','validate','-no-color')
        if (folder/'tests').exists():run('terraform',f'-chdir={folder}','test','-no-color')
    run(sys.executable,'-m','unittest','discover','-s','tests','-q')
    # Keep a venv interpreter's symlink path: resolving it escapes the venv.
    if args.app_python:run(args.app_python.absolute(),'-m','unittest','discover','-s','apps/streamlit/tests','-q')
    else:print('Streamlit tests not requested: supply --app-python with dependencies installed.')
    if args.agent_python:
        run(args.agent_python.absolute(),'-m','unittest','discover','-s','integration/aidp/tests','-q')
        if (ROOT/'integration/rag/tests').is_dir():
            run(args.agent_python.absolute(),'-m','unittest','discover','-s','integration/rag/tests','-q')
    else:print('Agent tests not requested: supply --agent-python with LangGraph installed.')
    print('Requested local checks passed. No OCI deployment or managed-runtime validation performed.')


if __name__=='__main__':main()
