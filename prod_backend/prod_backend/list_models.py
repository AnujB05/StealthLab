import asyncio
from app.config import settings


async def main():
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.general_compute_api_key,
        base_url=settings.general_compute_base_url,
    )
    models = await client.models.list()
    print(f"base_url: {settings.general_compute_base_url}")
    print(f"{len(models.data)} model(s) available to this key:\n")
    for m in models.data:
        print(" -", m.id)


asyncio.run(main())
