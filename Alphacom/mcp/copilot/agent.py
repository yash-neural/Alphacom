"""Minimal agent loop: OpenAI chat model + our MCP server over stdio.

Used by mcp/demo.py and mcp/evals/run_llm_evals.py. It is the MCP *client* side:
it starts the server, lists its tools, hands them to the model, executes the calls
the model makes, and answers the server's elicitation (confirmation) requests.
"""

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import mcp_types as t
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

MCP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gpt-5.4-mini"

# approve(summary) -> bool. The demo asks on the terminal; evals script the answer.
Approver = Callable[[str], Awaitable[bool]]


@dataclass
class ToolCall:
    name: str
    args: dict
    result: str
    is_error: bool
    approval_asked: str | None = None
    approved: bool | None = None


@dataclass
class Turn:
    user: str
    answer: str = ""
    calls: list[ToolCall] = field(default_factory=list)


def load_dotenv(path: Path = MCP_DIR.parent / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def terminal_approver(summary: str) -> bool:
    print("\n" + "=" * 70 + "\nCONFIRMATION REQUIRED\n" + summary + "\n" + "=" * 70)
    answer = await asyncio.to_thread(input, "Approve? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def fixed_approver(value: bool) -> Approver:
    async def approve(_: str) -> bool:
        return value

    return approve


class CopilotSession:
    def __init__(self, session: ClientSession, instructions: str, tools: list, model: str, approver: Approver):
        self.session = session
        self.model = model
        self.approver = approver
        self.openai_tools = [
            {"type": "function", "function": {"name": x.name, "description": x.description or "",
                                              "parameters": x.input_schema if hasattr(x, "input_schema") else x.inputSchema}}
            for x in tools
        ]
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self.messages: list[dict] = [{"role": "system", "content": (
            "You are the Support Copilot for internal support engineers. Answer using the tools; "
            "never invent case ids, customers, dates or numbers. Cite case ids. Be brief.\n"
            f"Today is {today}.\n\nTool server instructions:\n{instructions}")}]
        self._pending_approval: tuple[str, bool] | None = None
        from openai import AsyncOpenAI

        self.llm = AsyncOpenAI()

    async def on_elicit(self, _ctx, params) -> t.ElicitResult:
        approved = await self.approver(params.message)
        self._pending_approval = (params.message, approved)
        if approved:
            return t.ElicitResult(action="accept", content={"approve": True})
        return t.ElicitResult(action="decline")

    async def ask(self, question: str, max_steps: int = 8) -> Turn:
        turn = Turn(user=question)
        self.messages.append({"role": "user", "content": question})
        for _ in range(max_steps):
            resp = await self.llm.chat.completions.create(model=self.model, messages=self.messages,
                                                          tools=self.openai_tools)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                turn.answer = msg.content or ""
                self.messages.append({"role": "assistant", "content": turn.answer})
                return turn
            self.messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls]})
            for c in msg.tool_calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                self._pending_approval = None
                result = await self.session.call_tool(c.function.name, args)
                text = "\n".join(getattr(p, "text", "") for p in result.content)
                is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
                call = ToolCall(c.function.name, args, text, is_error)
                if self._pending_approval:
                    call.approval_asked, call.approved = self._pending_approval
                turn.calls.append(call)
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
        turn.answer = "(stopped: too many tool steps)"
        return turn


@asynccontextmanager
async def copilot(approver: Approver, *, model: str | None = None, user: str = "alice", env: dict | None = None):
    load_dotenv()
    params = StdioServerParameters(
        command=sys.executable, args=[str(MCP_DIR / "server.py")],
        env={**os.environ, "ALPHACOM_DEV_USER": user, **(env or {})},
    )
    holder: dict = {}

    async def elicit(ctx, p):
        return await holder["s"].on_elicit(ctx, p)

    async with stdio_client(params) as streams:
        async with ClientSession(*streams, elicitation_callback=elicit) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            s = CopilotSession(session, getattr(init, "instructions", "") or "", tools,
                               model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL), approver)
            holder["s"] = s
            yield s


def render_markdown(turns: list[Turn], model: str) -> str:
    out = [f"# Support Copilot transcript\n\nModel: `{model}`. Recorded {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.\n"]
    for turn in turns:
        out.append(f"## User\n\n> {turn.user}\n")
        for c in turn.calls:
            out.append(f"**Tool call:** `{c.name}`\n\n```json\n{json.dumps(c.args, indent=2)}\n```\n")
            if c.approval_asked:
                out.append(f"**Confirmation shown to user** ({'approved' if c.approved else 'declined'}):\n\n"
                           f"```\n{c.approval_asked}\n```\n")
            label = "Tool error" if c.is_error else "Tool result"
            body = c.result if len(c.result) < 2500 else c.result[:2500] + " …(truncated in transcript)"
            out.append(f"<details><summary>{label}</summary>\n\n```json\n{body}\n```\n</details>\n")
        out.append(f"## Copilot\n\n{turn.answer}\n")
    return "\n".join(out)
