import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_agent import validate_result


class Verification(unittest.TestCase):
    def messages(self):
        return [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call1", "function": {"name": "get_poc_inventory", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call1", "content": json.dumps({
                "synthetic": True, "gpus": 2, "gpu_model": "NVIDIA A10", "vram_gb_each": 24,
            })},
            {"role": "assistant", "content": "Synthetic inventory: two A10 GPUs, 48 GB total."},
        ]

    def test_accepts_real_linked_tool_roundtrip(self):
        report = validate_result({"messages": self.messages()})
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["tool_observed"])
        self.assertNotIn("messages", report)

    def test_plain_answer_is_not_proof(self):
        with self.assertRaises(ValueError):
            validate_result({"messages": [{"role": "assistant", "content": "48 GB total"}]})

    def test_unlinked_reply_is_not_proof(self):
        messages = self.messages(); messages[1]["tool_call_id"] = "different-call"
        with self.assertRaises(ValueError): validate_result({"messages": messages})

    def test_missing_final_answer_is_not_proof(self):
        with self.assertRaises(ValueError): validate_result({"messages": self.messages()[:-1]})

    def test_nonsynthetic_inventory_is_not_proof(self):
        messages = self.messages(); messages[1]["content"] = '{"synthetic": false}'
        with self.assertRaises(ValueError): validate_result({"messages": messages})


if __name__ == "__main__": unittest.main()
