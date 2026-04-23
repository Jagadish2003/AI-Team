import os
import requests
from dotenv import load_dotenv

class CustomApexAuthenticator:
    def __init__(self, apex_endpoint_url):
        self.apex_endpoint_url = apex_endpoint_url

    def request_token_from_apex(self, instance_url_to_send):
        """
        Hits the Apex class via GET request and passes the INSTANCE URL as a query parameter.
        """
        # This will be appended to the URL like: ?instanceUrl=https://...
        params = {
            "instanceUrl": instance_url_to_send
        }

        headers = {
            "Content-Type": "application/json"
        }

        try:
            print(f"Hitting Apex Endpoint: {self.apex_endpoint_url}")
            print(f"Sending GET Parameters: {params}\n")

            # CHANGED: Using requests.get() and params=params
            response = requests.get(self.apex_endpoint_url, params=params, headers=headers)

            response.raise_for_status()

            # Read the response from Salesforce
            token_response = response.json()

            print("✅ Successfully hit the Apex Class!")
            return token_response

        except requests.exceptions.HTTPError as err:
            print(f"❌ HTTP Error: {response.status_code}")
            print(f"Response Body: {response.text}")
        except Exception as e:
            print(f"❌ An error occurred: {e}")

        return None

# ==========================================
# EXECUTION LOGIC
# ==========================================
if __name__ == "__main__":
    load_dotenv()

    # Get variables from .env
    APEX_URL = os.getenv("APEX_REST_URL")
    TARGET_INSTANCE_URL = os.getenv("TARGET_INSTANCE_URL")

    if not APEX_URL or not TARGET_INSTANCE_URL:
        print("❌ ERROR: Please define APEX_REST_URL and TARGET_INSTANCE_URL in your .env file.")
        exit(1)

    apex_auth = CustomApexAuthenticator(APEX_URL)
    result = apex_auth.request_token_from_apex(TARGET_INSTANCE_URL)

    if result:
        print(f"\n🔑 Token returned from Apex: {result}")
