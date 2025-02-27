# cameras

# RTSP-to-WebRTC Webserver

This runs on a low power EC2 instance with a simple [docker compose script](rtsp-to-web/docker-compose.yml).

It runs an instance of https://github.com/deepch/RTSPtoWeb on port 8083. The [customer proxy](https://cp.u.doover.dev) reverse proxys this.

Authentication is handled by the main doover site, and validates the token passed is valid for the agent ID / stream.

A static IP is necessary for this server since tunnels are scoped down to only allow access to the IP address.

HTTP login and password keys are passed through from the main site when requesting a new stream (DON'T COMMIT THESE!!)

It currently limits webrtc ports to 60-61000, meaning there's a maximum of 1000 concurrent streams supported.

Some FIXMEs:
- Add `rtspwebviewer.u.doover.dev` to a Route53 hosted zone
- Setup the server behind a NLB (needs a static IP for tunnels)
- Auth currently doesn't work for staging. Either setup a seperate staging instance or sort out something else.
- HA / multiple instances / lots more tunnels.
