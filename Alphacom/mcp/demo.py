"""Ask the Support Copilot the sample question (and optionally a follow-up) with a real LLM.

    uv run --group demo python mcp/demo.py                 # sample question
    uv run --group demo python mcp/demo.py --follow-up     # ...then ask it to escalate one
    uv run --group demo python mcp/demo.py "your question"

Needs the stack running (docker compose up) and OPENAI_API_KEY in the environment
or in .env. Writes a markdown transcript to mcp/transcripts/.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from copilot.agent import copilot, fixed_approver, render_markdown, terminal_approver  # noqa: E402

SAMPLE = ("Which Platinum customers have P1 cases open for more than 48 hours "
          "and a renewal in the next 90 days?")
FOLLOW_UP = ("Escalate the Acme Corp one: the restore failures are blocking their finance team's "
             "month-end close.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=SAMPLE)
    ap.add_argument("--follow-up", action="store_true", help="then ask the copilot to escalate one case")
    ap.add_argument("--approve", choices=["ask", "yes", "no"], default="ask",
                    help="how to answer confirmation prompts (default: ask on the terminal)")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    approver = terminal_approver if args.approve == "ask" else fixed_approver(args.approve == "yes")
    questions = [args.question] + ([FOLLOW_UP] if args.follow_up else [])
    turns = []
    async with copilot(approver, model=args.model) as session:
        for q in questions:
            print(f"\nUSER: {q}")
            turn = await session.ask(q)
            for c in turn.calls:
                print(f"  -> {c.name}({c.args}){'  [error]' if c.is_error else ''}")
            print(f"\nCOPILOT: {turn.answer}")
            turns.append(turn)
        model = session.model

    out_dir = Path(__file__).parent / "transcripts"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}.md"
    path.write_text(render_markdown(turns, model), encoding="utf-8")
    print(f"\nTranscript: {path}")


if __name__ == "__main__":
    asyncio.run(main())
