import asyncio

from agents import Runner, TResponseInputItem

from agent import build_agent


async def main() -> None:
    assistant = build_agent()
    history: list[TResponseInputItem] = []
    print("Personal assistant ready. Type 'exit' to quit.")
    while True:
        message = input("> ").strip()
        if message.lower() in {"exit", "quit"}:
            break
        if not message:
            continue
        result = await Runner.run(
            assistant,
            history + [{"role": "user", "content": message}],
        )
        history = result.to_input_list()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
