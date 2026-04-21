import requests
import base64
import hashlib
import os

def generate_pkce():
    code_verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip('=')

    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip('=')

    return code_verifier, code_challenge

print("Generating PKCE...")

code_verifier, code_challenge = generate_pkce()

print("Code Challenge Generated!")

url = "https://test.salesforce.com/services/oauth2/authorize"

params = {
    "response_type": "code",
    "client_id": "3MVG9f8UqMdVrN_c_H._ZgevKKqRxjvd4X6fXlEYwGbwC7mij5Nzsi47sYLvEPkaaYXS3EYROFARpAUJia5Pc",
    "redirect_uri": "https://oauth.pstmn.io/v1/browser-callback",
    "code_challenge": code_challenge,
    "code_challenge_method": "S256"
}

print("Sending request to the URL...")

response = requests.get(url, params=params)

print("Response Received!")

print("URL:", response.url)
print("Response:", response.text)
