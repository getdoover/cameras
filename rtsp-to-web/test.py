import requests
from requests.auth import HTTPBasicAuth

base = f"http://3.25.33.247:8083/stream/joshphone"

resp = requests.get(f"http://3.25.33.247:8083/stream/joshphone2/info", auth=HTTPBasicAuth('doover', 'doover'))
print(resp.json())
# resp_data = resp.json()
print(resp)
