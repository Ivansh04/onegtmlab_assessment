
import asyncio

from app.config import get_settings
from app.clients.hubspot import HubSpotClient


async def main():
    settings = get_settings()
    client = HubSpotClient(settings)

    try:
        result = await client._request(
            "GET",
            "/crm/v3/objects/companies",
            params={
                "limit": 1,
                "properties": "name,domain",
            },
        )

        print("HubSpot authentication successful!")
        print("Companies returned:", len(result.get("results", [])))

    except Exception as exc:
        print(f"Test failed: {type(exc).__name__}: {exc}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
