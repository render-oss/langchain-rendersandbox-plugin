"""Repair a Python CSV program remotely, then verify with pristine tests and inputs."""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from lifecycle import sandbox_session

ORDERS = b"quantity,price\n2,10\n3,4\n1,10\n"
BROKEN = b"""import csv
import json

def total(path):
    with open(path, newline="") as source:
        rows = list(csv.DictReader(source))
    return sum(int(row["quantity"]) * int(row["price"]) for row in rows[:-1])

if __name__ == "__main__":
    with open("report.json", "w") as destination:
        json.dump({"total": total("orders.csv")}, destination)
"""
VISIBLE_TEST = b"""import unittest
from total import total

class TestTotal(unittest.TestCase):
    def test_orders(self):
        self.assertEqual(total("orders.csv"), 42)
"""
VERIFY_TEST = (
    VISIBLE_TEST
    + b"""
    def test_other_input(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "other.csv"
            path.write_text("quantity,price\\n0,999\\n3,7\\n")
            self.assertEqual(total(path), 21)

    def test_single_row(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "single.csv"
            path.write_text("quantity,price\\n4,5\\n")
            self.assertEqual(total(path), 20)
"""
)


async def repair_project(model: BaseChatModel) -> Path:
    """Run the repair and return its downloaded artifacts after sandbox cleanup.

    Accepting the model separately lets applications reuse their existing model
    client and lets the integration test exercise this exact walkthrough.
    """
    async with sandbox_session() as backend:
        mkdir = await backend.aexecute("mkdir -p /tmp/render-repair")
        if mkdir.exit_code != 0:
            raise RuntimeError("Could not create the remote project directory")
        uploads = await backend.aupload_files(
            [
                ("/tmp/render-repair/total.py", BROKEN),
                ("/tmp/render-repair/orders.csv", ORDERS),
                ("/tmp/render-repair/test_total.py", VISIBLE_TEST),
            ]
        )
        if any(result.error for result in uploads):
            raise RuntimeError(f"Fixture upload failed: {uploads}")
        before = await backend.aexecute(
            "cd /tmp/render-repair && python3 total.py && cat report.json"
        )
        if before.exit_code != 0 or json.loads(before.output) != {"total": 32}:
            raise RuntimeError("Fixture did not reproduce the expected 32 versus 42 bug")
        print("Confirmed the bug: total is 32; expected 42")
        agent = create_deep_agent(model=model, backend=backend)
        async with asyncio.timeout(300):
            await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Repair /tmp/render-repair/total.py. It skips an order when summing the CSV. "
                                "The supplied orders should total 42. Read the files and run the existing "
                                "test to observe the failure. Then fix the implementation, rerun its tests "
                                "and generate report.json. Do not change the input or tests."
                            ),
                        }
                    ]
                },
                config={"recursion_limit": 40},
            )
        # Do not trust an agent's final message or tests it could have edited.
        # Restore the original input and trusted tests before running them remotely.
        restored = await backend.aupload_files(
            [
                ("/tmp/render-repair/orders.csv", ORDERS),
                ("/tmp/render-repair/test_total.py", VERIFY_TEST),
            ]
        )
        if any(result.error for result in restored):
            raise RuntimeError("Could not restore verification inputs")
        verified = await backend.aexecute(
            "cd /tmp/render-repair && rm -rf __pycache__ && rm -f report.json "
            "&& python3 -m unittest -v test_total && python3 total.py"
        )
        if verified.exit_code != 0:
            raise RuntimeError(f"Independent remote verification failed:\n{verified.output}")
        files = await backend.adownload_files(
            [
                "/tmp/render-repair/total.py",
                "/tmp/render-repair/report.json",
            ]
        )
        if any(item.error or item.content is None for item in files):
            raise RuntimeError("Could not retrieve verified artifacts")
        report = files[1].content
        assert report is not None
        if json.loads(report) != {"total": 42}:
            raise RuntimeError("Downloaded report did not match the expected total")
        output = Path(".artifacts") / datetime.now(UTC).strftime("repair-%Y%m%dT%H%M%S%fZ")
        await asyncio.to_thread(output.mkdir, parents=True)
        for item in files:
            assert item.content is not None
            await asyncio.to_thread((output / Path(item.path).name).write_bytes, item.content)
        print(f"Verified 3 tests and total 42. Downloaded files to {output.resolve()}")
        # Generated Python is saved for inspection only, never executed on this host.
    return output


async def main() -> None:
    # This client and its model-provider key stay on the host. The sandbox gets
    # fixture bytes, shell commands and agent-generated files, not host credentials.
    model = init_chat_model(os.getenv("DEEP_AGENT_MODEL", "anthropic:claude-sonnet-4-6"))
    await repair_project(model)


if __name__ == "__main__":
    asyncio.run(main())
