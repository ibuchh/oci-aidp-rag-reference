"""Run on the actual agent AI Compute; report metadata without messages or secrets."""
import argparse
import asyncio
import json
import os
from pathlib import Path


def validate_result(result):
    messages = result.get("messages", [])
    calls = {
        call["id"]
        for message in messages if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
        if call.get("id") and call.get("function", {}).get("name") == "get_poc_inventory"
    }
    replies = [
        message for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") in calls
    ]
    inventory_observed = any(
        json.loads(message["content"]) == {
            "synthetic": True, "gpus": 2, "gpu_model": "NVIDIA A10", "vram_gb_each": 24,
        }
        for message in replies
    )
    final_answer = bool(messages and messages[-1].get("role") == "assistant"
                        and messages[-1].get("content") and not messages[-1].get("tool_calls"))
    if not calls or not inventory_observed or not final_answer:
        raise ValueError("A matching synthetic tool call, result and final answer are required")
    return {"status": "PASS", "tool_observed": True,
            "synthetic_inventory_verified": True, "final_answer_present": True}


async def verify():
    from agent import AgentBasic
    agent = AgentBasic()
    agent.setup()
    chat = await asyncio.to_thread(
        agent.client.run, "Reply with: Private AIDP inference verified."
    )
    if not chat.get("messages") or not chat["messages"][-1].get("content"):
        raise ValueError("Private chat did not return a final answer")
    result = await agent.invoke(
        "Use get_poc_inventory to report the synthetic GPU inventory and total GPU VRAM."
    )
    report = validate_result(result)
    report["private_chat_verified"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.config:
        os.environ["AIDP_VLLM_CONFIG"] = str(args.config.resolve())
    try:
        report = asyncio.run(verify())
    except Exception as error:
        # Exception messages from third-party libraries can contain request data.
        print(json.dumps({"status": "FAIL", "stage": "coded_agent",
                          "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
