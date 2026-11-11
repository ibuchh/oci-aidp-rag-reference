"""Build an auditable single-file AIDP entry with non-secret configuration."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {'base_url', 'private_dns_hostname', 'model', 'region', 'ca_pem',
           'api_key_credential_name', 'api_key_credential_secret_id', 'timeout_seconds', 'max_tokens'}


def render(config):
    if set(config)-ALLOWED or not all(config.get(k) for k in
            ('base_url','private_dns_hostname','model','region','ca_pem','api_key_credential_name','api_key_credential_secret_id')):
        raise ValueError('Use only the declared non-secret runtime configuration fields')
    if 'PRIVATE KEY' in config['ca_pem']:
        raise ValueError('Private keys cannot be embedded')
    client = ast.parse((ROOT/'private_client.py').read_text())
    agent = ast.parse((ROOT/'agent.py').read_text())
    verifier = ast.parse((ROOT/'verify_agent.py').read_text())
    nodes = [n for n in client.body if not isinstance(n, ast.If) and
             not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'main')]
    for node in agent.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'private_client':
            continue
        if isinstance(node, ast.ClassDef) and node.name == 'AgentBasic':
            node.name = '_InferenceAgent'
            # Keep a single discoverable setup/invoke entry class in AIDP.
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if method.name == 'setup': method.name = 'initialize'
                    elif method.name == 'invoke': method.name = 'infer'
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'EMBEDDED_CONFIG' for t in node.targets):
            node.value = ast.parse(repr(config), mode='eval').body
        nodes.append(node)
    nodes.extend(n for n in verifier.body if isinstance(n, ast.FunctionDef) and n.name == 'validate_result')
    wrapper = '''
class AgentBasic:
    def setup(self):
        self.agent = _InferenceAgent()
        self.agent.initialize()

    async def invoke(self, user_query: str, **kwargs):
        if user_query.strip() != "__POC_VERIFY__":
            return await self.agent.infer(user_query, **kwargs)
        try:
            chat = await asyncio.to_thread(self.agent.client.run, "Reply with: Private AIDP inference verified.")
            if not chat.get("messages") or not chat["messages"][-1].get("content"):
                raise ValueError("Missing private chat answer")
            result = await self.agent.infer("Use get_poc_inventory to report the synthetic GPU inventory and total GPU VRAM.")
            report = validate_result(result)
            report.update(private_chat_verified=True, stage="coded_agent")
        except Exception as error:
            report = {"status": "FAIL", "stage": "coded_agent", "error_type": type(error).__name__}
        return {"messages": [{"role": "assistant", "content": json.dumps(report)}]}
'''
    nodes.extend(ast.parse(wrapper).body)
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    source = '# Generated from integration/aidp source. Contains public trust and credential names only.\n'+ast.unparse(tree)+'\n'
    compile(source, 'inference_agent.generated.py', 'exec')
    return source
