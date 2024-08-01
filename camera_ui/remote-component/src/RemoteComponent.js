import RemoteAccess from 'doover_home/RemoteAccess'
import { ThemeProvider } from '@mui/material/styles';
import React, {useState, useEffect, Component} from 'react'
import { Joystick, JoystickShape } from 'react-joystick-component';
import {Paper, Grid, Box, Card, Button, Slider, Switch, FormControlLabel} from '@mui/material'
import LoadingButton from '@mui/lab/LoadingButton';
import WebRPCVideoPlayer from "./WebRPCVideoPlayer";


function getTunnelConfig(cameraUri) {
    return {
        "address": cameraUri,
        "protocol": "tcp",
        "allow_cidrs": ["3.25.33.247"],  // rtsp-to-web-proxy IP address. TODO: put this in a constant somewhere
        "timeout": 15.0,
    }
}

async function configureRPCVideoPlayer(camera_uri, agent_id) {
    // const url = "https://" + window.location.href.split('/')[2] + "/rtsp_playback/add";
    const url = "https://rtsptoweb.u.doover.com/rtsp_playback/add"
    console.log("fetching tunnel url", camera_uri, url);
    let result = await fetch(url,
        {
            method: 'POST',
            body: JSON.stringify({rtsp_uri: `rtsp://admin:19HandleyDrive@${camera_uri.replace("tcp://", "")}/live`, agent_id: agent_id}),
            headers: {'Content-Type': 'application/json'}
        });
    let data = await result.json();
    let urltoreturn = data.url + "?token=doover";
    console.log("rpc url is ", urltoreturn);
    return urltoreturn;

    // return result.json().then(data => data.url);
}

async function fetchTunnelConfigureRTC(agent_id, token, cam_uri) {
    let resp = await window.dooverDataAPIWrapper.get_channel_aggregate({
        channel_name: "tunnels",
        agent_id: agent_id,
    }, token)
    console.log(resp)
    let tunnel = resp.aggregate.payload.open.find(tunnel => tunnel.address === cam_uri);
    console.log("fetching tunnel url", tunnel?.url, tunnel)
    let url = tunnel?.url;
    this.tunnel_url = url;
    return url;
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
        }
        this.cam_uri = "rtsp://admin:19HandleyDrive@192.168.0.102:554/live";
        this.cam_base = "192.168.0.102:554";
        this.cam_address = this.get
        this.cam_name = this.getName()

        this.handleGetLiveView = this.handleGetLiveView.bind(this);
        this.teardownLiveView = this.teardownLiveView.bind(this);
        this.handleZoomSet = this.handleZoomSet.bind(this);
        this.handleZoomChange = this.handleZoomChange.bind(this);
        this.handlePtStop = this.handlePtStop.bind(this);
        this.handlePtMove = this.handlePtMove.bind(this);
        this.handleZoomMove = this.handleZoomMove.bind(this);

        this.sendControlCommand = this.sendControlCommand.bind(this);
        this.sendPtCmd = this.sendPtCmd.bind(this);

        // this.handleHumanNotifChange = this.handleHumanNotifChange.bind(this);
        // this.handleVehicleNotifChange = this.handleVehicleNotifChange.bind(this);
        // this.sendNotifCmd = this.sendNotifCmd.bind(this);
    }

    setupTunnel() {
        let ui_cmds_payload = {"cmds": {}}
        ui_cmds_payload["cmds"][`${this.cam_uri}_rtsp_enable`] = true;

        window.dooverDataAPIWrapper.post_channel_aggregate({
                channel_name: "ui_cmds",
                agent_id: this.state.agent_id,
            }, ui_cmds_payload, this.temp_token,
        );

        window.dooverDataAPIWrapper.post_channel_aggregate({
                channel_name: "tunnels",
                agent_id: this.state.agent_id,
            }, {to_open: [getTunnelConfig(this.cam_base)]}, this.temp_token
        ).then(
            setTimeout(() => fetchTunnelConfigureRTC(this.state.agent_id, this.temp_token, this.cam_base)
                .then(tunnel_url => configureRPCVideoPlayer(tunnel_url, this.state.agent_id))
                .then(url => this.setState({playerSource: url}))
                .then(() => this.setState({ showLiveView: true }))
                .then(() => this.setState({ loading: false }))
                .then(() => console.log("done"))
                    .then(() => this.forceUpdate())
            , 3000)
        );

    }

    teardownLiveView() {
        window.dooverDataAPIWrapper.post_channel_aggregate({
            channel_name: "tunnels",
            agent_id: this.state.agent_id,
        }, {to_close: [this.tunnel_url]}, this.temp_token);
        const url = "https://rtsptoweb.u.doover.com/rtsp_playback/remove"
        fetch(url, {
            method: 'POST',
            body: JSON.stringify({webrtc_uri: this.state.playerSource}),
            headers: {'Content-Type': 'application/json'}
        });
        this.setState({showLiveView: false});
    }

    handleGetLiveView() {
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

    // sendNotifCmd() {
    //     let fmt = (this.state.humanNotif ? "Human" : "") + ";" + (this.state.vehicleNotif ? "Vehicle" : "");
    //     this.sendUiCmd("ui_cmds", `${this.cam_name}_notif`, fmt);
    //     console.log("setting ptz notif to ", fmt);
    // }
    // handleHumanNotifChange = (event, enabled) => {
    //     this.setState({humanNotif: enabled}, () => this.sendNotifCmd());
    //     console.log("human new state:", enabled);
    // }
    //
    // handleVehicleNotifChange = (event, enabled) => {
    //     this.setState({vehicleNotif: enabled}, () => this.sendNotifCmd());
    //     console.log("vehicle new state:", enabled);
    // }


    render() {
        // const [loading, setLoading] = React.useState(false);
        // const [playerSource, setPlayerSource] = React.useState(null);
        // const [showLiveView, setShowLiveView] = React.useState(null);


        return (
            <Box
              // height={200}
              // width={200}
              // my={4}
              // display="flex"
              // alignItems="center"
              // gap={4}
              // p={2}
              // sx={{ border: '2px solid grey' }}
            >
                <div>{this.state.showLiveView}</div>
                {this.state.showLiveView ?
                    <Box padding={"20px"}>
                        <WebRPCVideoPlayer url={this.state.playerSource} />
                    </Box> : "test"
                }
                {/*<FormControlLabel control={<Switch defaultChecked />} label="Enable Live Recording"/>*/}
                {/*<FormControlLabel control={<Switch checked={this.state.humanNotif} onChange={this.handleHumanNotifChange} />} label="Human Notifications"/>*/}
                {/*<FormControlLabel control={<Switch checked={this.state.vehicleNotif} onChange={this.handleVehicleNotifChange} />} label="Vehicle Notifications"/>*/}
                <Joystick
                    size={100}
                    baseColor="blue"
                    stickColor="red"
                    throttle={200}
                    move={this.handlePtMove}
                    stop={this.handlePtStop}
                />
                <Joystick
                    size={100}
                    baseColor={"green"}
                    stickColor={"yellow"}
                    throttle={200}
                    controlPlaneShape={JoystickShape.AxisY}
                    move={this.handleZoomMove}
                    stop={this.handlePtStop}
                />
                <Slider
                    defaultValue={50}
                    aria-label="Zoom Control"
                    valueLabelDisplay="auto"
                    onChange={this.handleZoomChange}
                    onChangeCommitted={this.handleZoomSet}
                >
                    Zoom Control
                </Slider>
                <LoadingButton
                    variant="contained"
                    color="primary"
                    loading={this.state.loading}
                    onClick={this.handleGetLiveView}
                >
                    Enable Live View
                </LoadingButton>
                <Button
                    variant="contained"
                    color="primary"
                    onClick={this.teardownLiveView}
                >Close Live View</Button>
            </Box>
        );
    }
}
