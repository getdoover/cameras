import RemoteAccess from 'doover_home/RemoteAccess'
import React from 'react'
import { Joystick, JoystickShape } from 'react-joystick-component';
import {Box, Button, Slider, Typography} from '@mui/material'
import WebRPCVideoPlayer from "./WebRPCVideoPlayer";
import Stack from '@mui/material/Stack';
import {LoadingButton} from "@mui/lab";


function getTunnelConfig(cameraUri) {
    return {
        "address": cameraUri,
        "protocol": "tcp",
        "allow_cidrs": ["3.25.33.247"],  // rtsp-to-web-proxy IP address. TODO: put this in a constant somewhere
        "timeout": 15.0,
    }
}


export default class RemoteComponent extends RemoteAccess{

    constructor(props){
        super(props)
        this.state = {
            pending_update : {},
            agent_id: this.getUi().agent_key,
            loading: false,
            playerSource: null,
            showLiveView: false,
            zoom: 50,
            zoomDirection: null,
            ptDirection: null,
            vehicleNotif: false,
            humanNotif: false,

            cam_name: null,
            cam_base: null,
            cam_type: null,
            rtsp_uri: null,

            errorMessage: null,
        }

        this.handleGetLiveView = this.handleGetLiveView.bind(this);
        this.teardownLiveView = this.teardownLiveView.bind(this);
        this.handleZoomSet = this.handleZoomSet.bind(this);
        this.handleZoomChange = this.handleZoomChange.bind(this);
        this.handlePtStop = this.handlePtStop.bind(this);
        this.handlePtMove = this.handlePtMove.bind(this);
        this.handleZoomMove = this.handleZoomMove.bind(this);

        this.sendControlCommand = this.sendControlCommand.bind(this);
        this.sendPtCmd = this.sendPtCmd.bind(this);
        this.fetchTunnelConfigureRTC = this.fetchTunnelConfigureRTC.bind(this);
        this.configureRPCVideoPlayer = this.configureRPCVideoPlayer.bind(this);
        this.showError = this.showError.bind(this);

        // this.handleHumanNotifChange = this.handleHumanNotifChange.bind(this);
        // this.handleVehicleNotifChange = this.handleVehicleNotifChange.bind(this);
        // this.sendNotifCmd = this.sendNotifCmd.bind(this);
    }

    setupTunnel() {
        this.sendControlCommand("camera_control", "action", "rtsp_enable");
        console.log(this.cam_base);
        window.dooverDataAPIWrapper.post_channel_aggregate({
                channel_name: "tunnels",
                agent_id: this.state.agent_id,
            }, {to_open: [getTunnelConfig(this.cam_base)]}, this.temp_token
        ).then(
            setTimeout(() => this.fetchTunnelConfigureRTC(this.state.agent_id, this.temp_token, this.cam_base)
                .then(tunnel_url => tunnel_url ? this.configureRPCVideoPlayer(tunnel_url) : this.showError("Failed to setup connection to camera. Try again later."))
            , 3000)
        );
    }

    teardownLiveView() {
        window.dooverDataAPIWrapper.post_channel_aggregate({
            channel_name: "tunnels",
            agent_id: this.state.agent_id,
        }, {to_close: [{address: this.cam_base, url: this.tunnel_url}]}, this.temp_token);
        const url =  "https://" + window.location.href.split('/')[2] + "/camera_streams/" + this.state.agent_id;
        fetch(url, {
            method: 'DELETE',
            body: JSON.stringify({webrtc_uri: this.state.playerSource}),
            headers: {'Content-Type': 'application/json', 'Authorization': `Token ${this.temp_token}`}
        });
        this.setState({showLiveView: false});
    }

    showError(errorMessage) {
        this.setState({errorMessage: errorMessage});
    }

    async fetchTunnelConfigureRTC(agent_id, token, cam_uri) {
        let resp = await window.dooverDataAPIWrapper.get_channel_aggregate({
            channel_name: "tunnels",
            agent_id: agent_id,
        }, token)
        let tunnel = resp.aggregate.payload.open.find(tunnel => tunnel.address === cam_uri);
        console.log("fetching tunnel url", tunnel?.url, tunnel)
        let url = tunnel?.url;
        this.tunnel_url = url;
        return url;
    }

    async configureRPCVideoPlayer(tunnel_url) {
        const url =  "https://" + window.location.href.split('/')[2] + "/camera_streams/" + this.state.agent_id;
        let rtsp_uri = this.rtsp_uri.replace(this.cam_base, tunnel_url.replace("tcp://", ""));
        let result = await fetch(url,
            {
                method: 'POST',
                body: JSON.stringify({
                    cam_name: this.cam_name,
                    rtsp_uri: rtsp_uri,
                }),
                headers: {'Content-Type': 'application/json', 'Authorization': `Token ${this.temp_token}`}
            });
        let data = await result.json();
        if (!data.url) {
            this.showError("Failed to setup connection to streaming relay. Try again later.");
            return
        }
        let webrtc_url = data.url + "?token=" + this.temp_token;
        console.log("rpc url is ", webrtc_url, "for rtsp uri", rtsp_uri);
        this.setState({playerSource: webrtc_url});
        this.setState({loading: false});
        this.setState({showLiveView: true});
        // this.forceUpdate();
    }


    handleGetLiveView() {
        // fixme: can we stop this from reloading every 5secs??
        const state = this.getUiState();
        const reported = state.reported;
        console.log(reported);
        console.log(state);
        this.cam_base = reported.address + ":" + reported.port;
        this.cam_name = reported.name;
        this.rtsp_uri = reported.rtsp_uri;
        this.cam_type = reported.cam_type;
        // this.setState({
        //     cam_base: reported.address + ":" + reported.port,
        //     cam_name: reported.name,
        //     rtsp_uri: reported.rtsp_uri,
        //     cam_type: reported.cam_type
        // });

        this.setState({loading: true});
        if (this.temp_token === undefined) {
            window.dooverDataAPIWrapper.get_temp_token()
                .then(token => this.temp_token = token.token)
                .then(() => this.setupTunnel());
        } else {
            this.setupTunnel();
        }
    }

    handleZoomSet() {
        this.sendControlCommand("camera_control", "zoom", this.state.zoom);
    }

    handleZoomChange(event, newValue) {
        this.setState({zoom: newValue});
    }

    sendControlCommand(channel, key, val) {
        let payload = {[`${this.cam_name}`]: {[key]: val, task_id: window.crypto.randomUUID()}};
        if (this.temp_token === undefined) {
            window.dooverDataAPIWrapper.get_temp_token()
                .then(token => this.temp_token = token.token)
                .then(() => window.dooverDataAPIWrapper.post_channel_aggregate({
                        channel_name: channel,
                        agent_id: this.state.agent_id,
                        }, payload, this.temp_token,
                    ));
        } else {
            window.dooverDataAPIWrapper.post_channel_aggregate({
                channel_name: channel,
                agent_id: this.state.agent_id,
                }, payload, this.temp_token,
            );
        }

    }

    sendPtCmd(direction) {
        this.setState({ptDirection: direction});
        this.sendControlCommand("camera_control", "action", direction);
    }

    handlePtStop() {
        if (this.state.ptDirection !== null || this.state.zoomDirection !== null) {
            console.log("stopping");
            this.sendPtCmd("stop");
        }
    }

    handlePtMove(event) {
        if (event.direction !== this.state.ptDirection) {
            this.sendPtCmd(event.direction);
        }
    }

    handleZoomMove(event) {
        console.log(event)
        if (event.direction !== this.state.zoomDirection) {
            let transformed = event.direction === "FORWARD" ? "ZoomIn" : "ZoomOut";
            this.sendPtCmd(transformed);
        }
    }

    render() {

        let ptzControl = (
            <Stack direction="row" justifyContent="center">
                <div style={{ paddingRight: "10px" }}>
                    <Joystick
                        size={100}
                        baseColor="blue"
                        stickColor="red"
                        throttle={200}
                        move={this.handlePtMove}
                        stop={this.handlePtStop}
                    />
                    <Typography align="center">Pan/Tilt</Typography>
                </div>
                <div style={{ paddingLeft: "10px" }}>
                    <Joystick
                        size={100}
                        baseColor={"green"}
                        stickColor={"yellow"}
                        throttle={200}
                        controlPlaneShape={JoystickShape.AxisY}
                        move={this.handleZoomMove}
                        stop={this.handlePtStop}
                    />
                    <Typography align="center">Zoom</Typography>
                </div>
            </Stack>
        );
        let zoomSlider = (
            <Slider
                defaultValue={50}
                aria-label="Zoom Control"
                valueLabelDisplay="auto"
                onChange={this.handleZoomChange}
                onChangeCommitted={this.handleZoomSet}
            >
                Zoom Control
            </Slider>
        );

        let liveView = (
            <div>
                <Box padding={"20px"}>
                    <WebRPCVideoPlayer url={this.state.playerSource} />
                </Box>
                {this.cam_type && this.cam_type.includes('ptz') ? ptzControl : null}
                {this.cam_type && this.cam_type.includes('fixed') ? zoomSlider : null}
            </div>
        )

        let enableButton = (
            <LoadingButton
                variant="contained"
                color="primary"
                onClick={this.handleGetLiveView}
                loading={this.state.loading}
            >
                Enable Live View
            </LoadingButton>
        );

        let disableButton = (
            <Button
                variant="contained"
                color="primary"
                onClick={this.teardownLiveView}
            >
                Close Live View
            </Button>
        );

        if (this.state.errorMessage) {
            return (
                <Box>
                    <Box width = 'fit-content' margin = '4px' style={{
                            'font': '400 1.2rem/1.5 "Roboto","Helvetica","Arial",sans-serif',
                            color: 'rgb(0, 0 , 0, 0.54)',
                            paddingTop: "30px",
                            paddingBottom: "30px",
                            margin: "auto",
                        }}
                    >
                        {this.state.errorMessage}
                    </Box>
                    <LoadingButton
                        variant="contained"
                        color="primary"
                        onClick={this.handleGetLiveView}
                        loading={this.state.loading}
                    >
                        Try Again
                    </LoadingButton>
                </Box>
            )
        }

        return (
            <Box>
                {this.state.showLiveView ? liveView : null}
                <Box textAlign="center" padding={"20px"}>
                    {this.state.showLiveView ? disableButton : enableButton}
                </Box>
            </Box>
        );
    }
}
