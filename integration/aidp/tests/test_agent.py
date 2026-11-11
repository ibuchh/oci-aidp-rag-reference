import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(importlib.util.find_spec("langgraph"), "Install the isolated graph test dependencies")
class AgentTest(unittest.TestCase):
    def test_default_runtime_config_is_adjacent_to_agent_file(self):
        import agent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime.json").write_text(json.dumps({"model": "qwen3-8b", "ca_file": "inference-ca.crt"}))
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(agent, "__file__", str(root / "agent.py")), \
                    patch.object(agent, "PrivateClient") as client:
                instance = agent.AgentBasic()
                instance.setup()
                self.assertEqual(client.call_args.args[0]["ca_file"], str(root.resolve() / "inference-ca.crt"))

    def test_documented_entry_interface_runs_real_graph_without_cross_request_history(self):
        import agent
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({"model": "qwen3-8b", "ca_file": "inference-ca.crt"}))
            with patch.dict(os.environ, {"AIDP_VLLM_CONFIG": str(path)}), \
                    patch.object(agent, "PrivateClient") as client:
                client.return_value.run.side_effect = lambda prompt, **kw: {
                    "messages": [{"role": "assistant", "content": prompt}], "tool_observed": False,
                }
                instance = agent.AgentBasic()
                instance.setup()
                self.assertEqual(client.call_args.args[0]["ca_file"], str(Path(directory).resolve() / "inference-ca.crt"))
                first = asyncio.run(instance.invoke("first synthetic request"))
                second = asyncio.run(instance.invoke("second synthetic request"))
                self.assertEqual(first["messages"][0]["content"], "first synthetic request")
                self.assertEqual(second["messages"], [{"role": "assistant", "content": "second synthetic request"}])
                self.assertEqual(client.return_value.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
