import json
import re
from urllib.parse import quote

import requests

from flask import Flask, request
from requests.auth import HTTPBasicAuth
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

matcher = re.compile(r".*/stream/(?P<agent_id>.*)/channel/(?P<channel_id>.*)/webrtc")


@app.route("/", methods=['GET', 'POST'])
def hello_world():
    # example json format:
    # {'proto': 'WebRTC', 'stream': 'joshphone', 'channel': '0', 'token': 'doover', 'ip': '111.220.50.243'}
    ok = request.json.get("token") == "doover"
    print(f"Got request: {request.json}, ok: {ok}")
    return json.dumps({"status": "1" if ok else "0"})


@app.route("/rtsp_playback/add", methods=['POST'])
def rtsp_playback_add():
    # we have to do 2 api calls either way...

    data = request.json
    agent_id = data.get("agent_id", "")
    channel_payload = {
        "name": data.get("rtsp_uri"),
        "url": data.get("rtsp_uri"),
        "on_demand": True,
        "debug": False,
    }
    base = f"https://rtsptowebviewer.u.doover.com/stream/{quote(agent_id)}"
    auth = HTTPBasicAuth("doover", "doover")

    resp = requests.get(f"{base}/info", auth=auth)
    resp_data = resp.json()
    if resp.status_code != 200 or resp_data.get("status") != 1:
        # stream doesn't exist, create it...
        body = {"name": agent_id, "channels": {"0": channel_payload}}
        requests.post(f"{base}/add", json=body, auth=auth)
        channel_id = 0
    else:
        exists = [ch for ch, val in resp_data["payload"]["channels"].items() if val.get("url") == data.get("rtsp_url")]
        if not exists:
            # channel doesn't exist, just add a new one...
            channel_num = len(resp_data["payload"]["channels"])
            requests.post(
                f"{base}/channel/{channel_num}/add",
                json=channel_payload,
                auth=auth
            )
            channel_id = channel_num
        else:
            # stream and channel URL exists, no need to do anything...
            channel_id = exists[0]

    return json.dumps({"url": f"{base}/channel/{channel_id}/webrtc"})


@app.route("/rtsp_playback/remove", methods=['POST'])
def rtsp_playback_remove():
    # we have to do 2 api calls either way...

    data = request.json
    match = matcher.match(data.get("webrtc_uri", ""))
    if not match:
        return

    url = f"https://rtsptowebviewer.u.doover.com/stream/{quote(match['agent_id'])}/channel/{match['channel_id']}/delete"
    auth = HTTPBasicAuth("doover", "doover")

    requests.get(url, auth=auth)
    return json.dumps({"status": "200"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
