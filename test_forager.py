
import asyncio

from app.config import get_settings
from app.clients.forager import ForagerClient


async def main():
    settings = get_settings()
    client = ForagerClient(settings)

    try:
        results = await client.search_companies(domain="openai.com")

        print("Search successful")
        print("Results:", len(results))

        for index, organization in enumerate(results, start=1):
            print(f"\n--- Result {index} ---")
            print("Result keys:", list(organization.keys()))

            organization = organization.get("organization", organization)

            if not isinstance(organization, dict):
                print("Unexpected organization format")
                continue
            print("ID:", organization.get("id"))
            print("Name:", organization.get("name"))
            print("Legal name:", organization.get("legal_name"))
            print("Domain:", organization.get("domain"))
            print("Website:", organization.get("website"))
            print("Description:", organization.get("description"))
            print("Founded:", organization.get("founded_date"))
            print("Operating status:", organization.get("operating_status"))
            print("Employee range:", organization.get("employees_range"))
            print("Employee count:", organization.get("employees_amount"))
            print("Location:", organization.get("location"))
            print("Finance info:", organization.get("finance_info"))
            print("Addresses:", organization.get("addresses"))
            print("LinkedIn info:", organization.get("linkedin_info"))
            print("Keywords:", organization.get("keywords"))
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
