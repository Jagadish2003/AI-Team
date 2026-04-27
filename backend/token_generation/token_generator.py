import time
import jwt
import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://test.salesforce.com"
TOKEN_URL = LOGIN_URL + "/services/oauth2/token"

CLIENT_ID = os.environ.get("SF_CLIENT_ID")
USERNAME = os.environ.get("SF_USER")
PRIVATE_KEY_PATH = "token_generation/server.key"

TOKEN_FILE = "token_generation/sf_token.json"

with open(PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY = f.read()


# -----------------------------
# SAVE TOKEN TO FILE
# -----------------------------
def save_token(access_token, instance_url):
    data = {
        "access_token": access_token,
        "instance_url": instance_url,
        "timestamp": time.time()
    }

    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)


# -----------------------------
# LOAD TOKEN FROM FILE
# -----------------------------
def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None

    with open(TOKEN_FILE, "r") as f:
        return json.load(f)


# -----------------------------
# GENERATE NEW TOKEN
# -----------------------------
def get_new_token():
    payload = {
        "iss": CLIENT_ID,
        "sub": USERNAME,
        "aud": LOGIN_URL,
        "exp": int(time.time()) + 300
    }

    encoded_jwt = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")

    response = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": encoded_jwt
    })

    if response.status_code != 200:
        raise Exception(f"Token generation failed: {response.text}")

    data = response.json()

    access_token = data["access_token"]
    instance_url = data["instance_url"]

    # ✅ SAVE TO FILE
    save_token(access_token, instance_url)

    return access_token, instance_url


# -----------------------------
# GET VALID TOKEN
# -----------------------------
def get_token():
    token_data = load_token()

    # If no file → create new token
    if not token_data:
        return get_new_token()

    # Optional: refresh after ~2 hours
    if time.time() - token_data["timestamp"] > 7000:
        return get_new_token()

    return token_data["access_token"], token_data["instance_url"]


# -----------------------------
# MAKE API CALL
# -----------------------------
def make_request(url, access_token):
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    response = requests.get(url, headers=headers)

    # If expired → regenerate
    if response.status_code == 401:
        print("Token expired. Refreshing...")

        access_token, instance_url = get_new_token()

        headers["Authorization"] = f"Bearer {access_token}"
        response = requests.get(url, headers=headers)

        return response, access_token, instance_url

    return response, access_token, None


# -----------------------------
# MAIN FLOW
# -----------------------------
def main():
    access_token, instance_url = get_token()

    query = "SELECT Id, Name FROM ApexClass LIMIT 5"
    tooling_url = (
        instance_url +
        "/services/data/v61.0/tooling/query/?q=" +
        query.replace(" ", "+")
    )

    response, access_token, new_instance = make_request(tooling_url, access_token)

    # If token was refreshed, use updated instance_url
    if new_instance:
        instance_url = new_instance

    return access_token, instance_url

# -----------------------------
# ENTRY POINT
# -----------------------------
if __name__ == "__main__":
    main()
