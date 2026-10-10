# langchain-rendersandbox-plugin

`langchain-render` runs LangChain Deep Agents' shell and file tools in [Render Sandboxes](https://render.com/docs/sandboxes). Your agent runs in your application; commands and files live in the remote sandbox.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A [Render workspace with Sandboxes enabled](https://render.com/docs/sandboxes#1-confirm-access) (early access)
- Claude Sonnet 4.6 for the example

## Install the review build

Clone this repository and install the review build:

```bash
git clone https://github.com/render-oss/langchain-rendersandbox-plugin.git
cd langchain-rendersandbox-plugin
uv sync --frozen --group dev
```

Copy [.env.example](.env.example) to `.env` in your editor and fill in:

| Variable | Purpose and setup |
| --- | --- |
| `RENDER_API_KEY` | [API key](https://render.com/docs/api#1-create-an-api-key) with access to your Render workspace |
| `RENDER_WORKSPACE_ID` | Selects the sandbox's workspace; find its ID in [Dashboard](https://dashboard.render.com/) workspace Settings |
| `ANTHROPIC_API_KEY` | [Model credentials](https://docs.langchain.com/oss/python/integrations/chat/anthropic) for Claude |

`.env` is gitignored. Credentials stay in your application; the agent sends task content and tool results to the model.

## Quickstart

Save as `quickstart.py` in the checkout root:

```python
import asyncio

from deepagents import create_deep_agent
from langchain_render import sandbox_session


async def main():
    async with sandbox_session() as backend:
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-6",
            backend=backend,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Create /tmp/hello.py and run it."}]}
        )
        print(result["messages"][-1].content)


asyncio.run(main())
```

```bash
uv run --env-file .env python quickstart.py
```

The helper creates a sandbox, waits for readiness, and verifies termination on exit, including after errors. Defaults are a 15-minute lifetime and deny-all outbound networking. Download wanted files before leaving the block; termination removes them.

## Documentation

- [Integration guide](docs/guide.mdx): repair a Python program and verify the result.
- [Examples](examples): connection check and complete agent workflow.
- [Configuration and existing sandboxes](VALIDATION.md#configuration-and-existing-sandboxes), plus [runtime behavior](VALIDATION.md#execution-and-file-behavior).
- [Development and testing](VALIDATION.md#reproduce-the-internal-review-build).

[Apache-2.0](LICENSE).
