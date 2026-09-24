import asyncio
import os
from pathlib import Path
from time import perf_counter

from agents import Agent, Runner
from dotenv import load_dotenv


# 始终读取脚本同目录的 .env，不受终端当前目录影响。
# 以 .env 为准，覆盖终端中已有的同名环境变量。
load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=True)

if not os.getenv("OPENAI_API_KEY", "").strip():
    raise RuntimeError("未配置 OPENAI_API_KEY，请填写脚本同目录的 .env 文件。")

agent = Agent(
    name="History tutor",
    instructions="You answer history questions clearly and concisely.",
    model="gpt-5-nano",
)


async def main() -> None:
    started_at = perf_counter()
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    elapsed_seconds = perf_counter() - started_at
    print(result.final_output)
    print(f"\n本次响应耗时：{elapsed_seconds:.2f} 秒")


if __name__ == "__main__":
    asyncio.run(main())
