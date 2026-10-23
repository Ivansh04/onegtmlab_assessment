
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
                "limit": 100,
                "properties": "name,domain,description,numberofemployees",
            },
        )

        companies = result.get("results", [])
        print(f"Companies retrieved: {len(companies)}")

        for company in companies:
            properties = company.get("properties", {})
            print(
                f"ID: {company.get('id')} | "
                f"Name: {properties.get('name')} | "
                f"Domain: {properties.get('domain')}"
            )

    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
