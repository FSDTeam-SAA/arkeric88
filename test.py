# import requests
# import json
# import os
# from dotenv import load_dotenv
# load_dotenv()  # Load environment variables from .env file



# API_KEY = os.getenv("VIATOR_API_KEY")
# PARTNER_ID = os.getenv("VIATOR_PARTNER_ID")

# BASE_URL = "https://api.sandbox.viator.com/partner"

# headers = {
#     "exp-api-key": API_KEY,
#     "Accept-Language": "en-US",
#     "Accept": "application/json;version=2.0",
# }


# def test_destinations():
#     url = f"{BASE_URL}/destinations"

#     response = requests.get(
#         url,
#         headers=headers,
#         timeout=30
#     )

#     print("Status Code:", response.status_code)

#     if response.status_code == 200:
#         data = response.json()

#         print("API Connected Successfully ✅")

#         # Print only first 5 destinations
#         destinations = data.get("destinations", [])

#         for destination in destinations[:5]:
#             print("-" * 50)
#             print("ID:", destination.get("destinationId"))
#             print("Name:", destination.get("name"))
#             print("Type:", destination.get("type"))
#             print("Currency:", destination.get("defaultCurrencyCode"))
#             print("Location:", destination.get("center"))

#     else:
#         print("API Error ❌")
#         print(response.text)


# if __name__ == "__main__":
#     test_destinations()

import os

import requests
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    load_dotenv(".env.viator.sandbox")

    # This endpoint belongs to Viator's sandbox. Keep sandbox and production
    # credentials separate: the production client uses VIATOR_API_KEY.
    api_key = os.getenv("VIATOR_SANDBOX_API_KEY")
    base_url = os.getenv("VIATOR_SANDBOX_BASE_URL", "https://api.sandbox.viator.com/partner").rstrip("/")

    if not api_key:
        raise RuntimeError("Set VIATOR_SANDBOX_API_KEY in .env.viator.sandbox before running this script.")

    response = requests.get(
        f"{base_url}/products/modified-since",
        headers={
            "exp-api-key": api_key,
            "Accept-Language": "en-US",
            "Accept": "application/json;version=2.0",
        },
        params={"count": 5},
        timeout=30,
    )

    print("Status Code:", response.status_code)
    print(response.text)


if __name__ == "__main__":
    main()
