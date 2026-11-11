"""AIDP code-authoring entry file; runtime compatibility is pending a deployed test."""
import asyncio
import json
import os
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from private_client import PrivateClient

EMBEDDED_CONFIG = None


class State(TypedDict):
    user_query: str
    messages: list[dict]


class AgentBasic:
    def setup(self):
        if EMBEDDED_CONFIG is not None:
            config = dict(EMBEDDED_CONFIG)
        else:
            default_config = Path(__file__).resolve().with_name("runtime.json")
            config_path = Path(os.environ.get("AIDP_VLLM_CONFIG", str(default_config))).resolve()
            config = json.loads(config_path.read_text())
            for field in ("ca_file", "api_key_file"):
                if config.get(field) and not Path(config[field]).is_absolute():
                    config[field] = str(config_path.parent / config[field])
        self.client = PrivateClient(config)
        graph = StateGraph(State)
        graph.add_node("private_vllm", self._infer)
        graph.add_edge(START, "private_vllm")
        graph.add_edge("private_vllm", END)
        self.graph = graph.compile()

    async def _infer(self, state):
        result = await asyncio.to_thread(self.client.run, state["user_query"], tools=True)
        return {"messages": result["messages"]}

    async def invoke(self, user_query: str, **kwargs):
        # One invocation owns its messages. No persistent chat or database writes.
        if not hasattr(self, "graph"):
            raise RuntimeError("Call setup before invoke")
        return await self.graph.ainvoke({"user_query": user_query, "messages": []})
