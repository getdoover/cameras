import RemoteAccess from 'doover_home/RemoteAccess'
import React from 'react'
import { Joystick, JoystickShape } from 'react-joystick-component';
import {
    Box,
    Button, ButtonGroup, createMuiTheme,
    Dialog, DialogActions,
    DialogContent,
    DialogContentText,
    DialogTitle, FormControlLabel, IconButton, Radio, RadioGroup,
    Slider,
    TextField,
    Typography
} from '@mui/material'
import WebRPCVideoPlayer from "./WebRPCVideoPlayer";
import Stack from '@mui/material/Stack';
import {LoadingButton} from "@mui/lab";
import DeleteIcon from '@mui/icons-material/Delete';
import ZoomInIcon from '@mui/icons-material/ZoomIn';
import ZoomOutIcon from '@mui/icons-material/ZoomOut';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import KeyboardArrowLeftIcon from '@mui/icons-material/KeyboardArrowLeft';
import KeyboardArrowRightIcon from '@mui/icons-material/KeyboardArrowRight';
import {createTheme, ThemeProvider} from "@mui/system";
import AddIcon from '@mui/icons-material/Add';
import PropTypes from "prop-types";


function getTunnelConfig(cameraUri) {
    return {
        "address": cameraUri,
        "protocol": "tcp",
        "allow_cidrs": ["3.25.33.247"],  // rtsp-to-web-proxy IP address. TODO: put this in a constant somewhere
        "timeout": 15.0,
    }
}

const rtl_theme = createTheme({
  direction: 'rtl',
});


function preventHorizontalKeyboardNavigation(event) {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
    }
}

function ConfirmationDialogRaw(props) {
  const { onClose, open, options, ...other } = props;
  const [value, setValue] = React.useState(options[0]);
  const radioGroupRef = React.useRef(null);

  const handleEntering = () => {
    if (radioGroupRef.current != null) {
      radioGroupRef.current.focus();
    }
  };

  const handleCancel = () => {
    onClose();
  };

  const handleOk = () => {
    onClose(value);
  };

  const handleChange = (event) => {
    setValue(event.target.value);
  };

  return (
    <Dialog
      sx={{ '& .MuiDialog-paper': { width: '80%', maxHeight: 435 } }}
      maxWidth="xs"
      TransitionProps={{ onEntering: handleEntering }}
      open={open}
      {...other}
    >
      <DialogTitle>Delete Preset</DialogTitle>
      <DialogContent dividers>
        <RadioGroup
          ref={radioGroupRef}
          value={value}
          onChange={handleChange}
        >
          {options.map((option) => (
            <FormControlLabel
              value={option}
              key={option}
              control={<Radio />}
              label={option}
            />
          ))}
        </RadioGroup>
      </DialogContent>
      <DialogActions>
        <Button autoFocus onClick={handleCancel}>
          Cancel
        </Button>
        <Button onClick={handleOk}>Ok</Button>
      </DialogActions>
    </Dialog>
  );
}

ConfirmationDialogRaw.propTypes = {
    onClose: PropTypes.func.isRequired,
    open: PropTypes.bool.isRequired,
    options: PropTypes.array.isRequired,
};


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
            camPresets: [],

            pan: 0,
            tilt: 0,

            allow_absolute_position: true,
            is_ui_sync_active: false,

            deletePresetDialogOpen: false,
            createPresetDialogOpen: false,
        }

        this.handleGetLiveView = this.handleGetLiveView.bind(this);
        this.teardownLiveView = this.teardownLiveView.bind(this);
        this.handleZoomSet = this.handleZoomSet.bind(this);
        this.handleZoomChange = this.handleZoomChange.bind(this);
        this.handlePtStop = this.handlePtStop.bind(this);
        this.handlePtMove = this.handlePtMove.bind(this);
        this.sendControlCommand = this.sendControlCommand.bind(this);
        this.fetchTunnelConfigureRTC = this.fetchTunnelConfigureRTC.bind(this);
        this.configureRPCVideoPlayer = this.configureRPCVideoPlayer.bind(this);
        this.showError = this.showError.bind(this);

        this.createPreset = this.createPreset.bind(this);
        this.deletePreset = this.deletePreset.bind(this);

        this.handlePresetDialogClose = this.handlePresetDialogClose.bind(this);
        this.handlePresetDialogOpen = this.handlePresetDialogOpen.bind(this);
        this.handleDeletePresetDialogOpen = this.handleDeletePresetDialogOpen.bind(this);
        this.handleDeletePresetDialogClose = this.handleDeletePresetDialogClose.bind(this);

        this.sendZoomValue = this.sendZoomValue.bind(this);
        this.sendPtAbsolute = this.sendPtAbsolute.bind(this);
        this.gotoPreset = this.gotoPreset.bind(this);

        this.handlePtWrapSlider = this.handlePtWrapSlider.bind(this);

        this.syncUi = this.syncUi.bind(this);
        this.intervalSyncUi = this.intervalSyncUi.bind(this);
        this.resetPlayerUrl = this.resetPlayerUrl.bind(this);

        this.pt_interval_id = null;
        this.sync_ui_interval_id = null;

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
        this.setState({errorMessage: errorMessage, loading: false});
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
        this.setState({
            playerSource: webrtc_url,
            loading: false,
            errorMessage: null,
            showLiveView: true,
        });
        // this.forceUpdate();
    }

    syncUi() {
        // fixme: can we stop this from reloading every 5secs??
        // this.setState({
        //     cam_base: reported.address + ":" + reported.port,
        //     cam_name: reported.name,
        //     rtsp_uri: reported.rtsp_uri,
        //     cam_type: reported.cam_type
        // });

        console.log("syncing ui...")
        const state = this.getUiState();
        const reported = state.reported;
        console.log(state);
        this.cam_base = reported.address + ":" + reported.port;
        this.cam_name = reported.name;
        this.rtsp_uri = reported.rtsp_uri;
        this.cam_type = reported.cam_type;
        this.is_fixed_cam = this.cam_type && this.cam_type.includes('fixed');
        let presets = reported?.presets || [];
        let position = reported?.cam_position;
        console.log(position?.x, position?.y, position?.zoom)
        let allow = reported?.allow_absolute_position;
        if (allow === undefined) {
            allow = this.state.allow_absolute_position;
        }

        this.setState({
            loading: true,
            camPresets: presets,
            zoom: position?.zoom || 0,
            pan: position?.pan || 0,
            tilt: position?.tilt || 0,
            allow_absolute_position: allow,
        });
    }


    handleGetLiveView() {
        this.syncUi();

        if (this.temp_token === undefined) {
            window.dooverDataAPIWrapper.get_temp_token()
                .then(token => this.temp_token = token.token)
                .then(() => this.setupTunnel());
        } else {
            this.setupTunnel();
        }
    }

    handleZoomSet() {
        // this.sendControlCommand("camera_control", {"action": "zoom", "value": this.state.zoom});
    }

    sendZoomValue() {
        this.sendControlCommand("camera_control", {"action": "zoom", "value": this.state.zoom});
        if (this.cam_type && this.cam_type.includes('fixed')) {
            this.setState({allow_absolute_position: false});
            this.intervalSyncUi(5000, 200, true);
        }
    }

    sendPtAbsolute() {
        this.sendControlCommand("camera_control", {"action": "pantilt_absolute", "value": {"pan": -this.state.pan, "tilt": this.state.tilt}});
        this.setState({allow_absolute_position: false});
        this.intervalSyncUi(5000, 200, true);
    }

    handleZoomChange(event, newValue) {
        this.setState(
            {zoom: newValue},
            this.sendZoomValue
        );
    }

    sendControlCommand(channel, payload) {
        payload["task_id"] = window.crypto.randomUUID();
        let to_send = {[`${this.cam_name}`]: payload};
        if (this.temp_token === undefined) {
            window.dooverDataAPIWrapper.get_temp_token()
                .then(token => this.temp_token = token.token)
                .then(() => window.dooverDataAPIWrapper.post_channel_aggregate({
                        channel_name: channel,
                        agent_id: this.state.agent_id,
                        }, to_send, this.temp_token,
                    ));
        } else {
            window.dooverDataAPIWrapper.post_channel_aggregate({
                channel_name: channel,
                agent_id: this.state.agent_id,
                }, to_send, this.temp_token,
            );
        }

    }

    handlePtStop() {
        if (this.pt_interval_id !== null) {
            this.intervalSyncUi(5000, 200, true);
            clearInterval(this.pt_interval_id);
            this.pt_interval_id = null;
            this.sendControlCommand("camera_control", {"action": "stop"});
        }
        if (this.sync_ui_interval_id !== null) {
            clearInterval(this.sync_ui_interval_id);
            this.sync_ui_interval_id = null;
        }
    }

    handlePtMove(event) {
        this.last_pt_event = event;
        // let payload = {"action": "pantilt", "value": {"pan": event.x, "tilt": event.y}};
        // this.sendControlCommand("camera_control", payload);

        // so we get a smoother pan/tilt, this sends the previous command every 200ms no matter if it has changed or not.
        if (this.pt_interval_id === null) {
            this.setState({allow_absolute_position: false});
            this.pt_interval_id = setInterval(() => {
                let payload = {"action": "pantilt_continuous", "value": {"pan": this.last_pt_event.x, "tilt": this.last_pt_event.y}};
                this.sendControlCommand("camera_control", payload);
            }, 200);
        }
        if (this.sync_ui_interval_id === null) {
            // this.sync_ui_interval_id = setInterval(() => {
            //     // this.sendControlCommand("camera_control", {"action": "sync_ui"});
            //     this.syncUi();  // yes, this will use the previous sync data, but better than nothing...
            // }, 200)
        }
    }

    intervalSyncUi(timeout = 5000, interval = 200, until_allowed = false) {
        if (this.state.is_ui_sync_active) {
            return;
        }
        this.setState({is_ui_sync_active: true});
        let interval_id = setInterval(() => {
                // if (until_allowed && this.state.allow_absolute_position === true) {
                //     return;
                // }
                this.syncUi();
            }, interval);
        setTimeout(() => { clearInterval(interval_id); }, timeout);
        setTimeout(() => { this.setState({is_ui_sync_active: false, allow_absolute_position: true}); }, timeout + 50);
    }

    createPreset(name) {
        let presets = this.state.camPresets;
        presets.push(name);
        this.setState({camPresets: presets});
        this.sendControlCommand("camera_control", {
            "action": "create_preset",
            "value": name,
        });
    }

    deletePreset(name) {
        let presets = this.state.camPresets;
        presets = presets.filter(preset => preset !== name);
        this.setState({camPresets: presets});
        this.sendControlCommand("camera_control", {
            "action": "delete_preset",
            "value": name,
        });
    }

    handlePresetDialogOpen() {
        this.setState({createPresetDialogOpen: true});
    }

    handlePresetDialogClose() {
        this.setState({createPresetDialogOpen: false});
    }

    handleDeletePresetDialogOpen() {
        this.setState({deletePresetDialogOpen: true});
    }

    handleDeletePresetDialogClose(value) {
        this.setState({deletePresetDialogOpen: false});

        if (value) {
            this.deletePreset(value)
        }
    }

    gotoPreset(preset) {
        this.sendControlCommand("camera_control", {"action": "goto_preset", "value": preset})
        this.setState({allow_absolute_position: false})
        this.intervalSyncUi(5000, 200, true);
    }

    handlePtWrapSlider(key, increment, min, max) {
        return () => {
            let newVal = this.state[key] + increment;
            if (newVal < min) {
                newVal = max;
            } else if (newVal > max) {
                newVal = min;
            }
            this.setState({[key]: newVal}, this.sendPtAbsolute);
        }
    }

    resetPlayerUrl() {
        let url = this.state.playerSource;
        this.setState({playerSource: null}, () => { this.setState({playerSource: url}) });
    }

    render() {
        let tiltSlider = (
            <Stack spacing={2} direction="column" sx={{ mb: 1 }} alignItems="center">
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={this.handlePtWrapSlider("tilt", 0.1, -1, 1)}>
                    <KeyboardArrowUpIcon />
                </IconButton>
                <Slider
                  sx={{
                    '& input[type="range"]': {
                      WebkitAppearance: 'slider-vertical',
                    },
                  }}
                  disabled={!this.state.allow_absolute_position}
                  orientation="vertical"
                  step={0.1}
                  min={-1}
                  max={1}
                  valueLabelDisplay="auto"
                  onKeyDown={preventHorizontalKeyboardNavigation}
                  onChange={(event, new_value) => this.setState({tilt: new_value}, this.sendPtAbsolute)}
                  value={this.state.tilt}
                />
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={this.handlePtWrapSlider("tilt", -0.1, -1, 1)}>
                  <KeyboardArrowDownIcon />
                </IconButton>
            </Stack>
        );

        let panSlider = (
            <Stack direction="row" margin="0">
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={this.handlePtWrapSlider("pan", -0.1, -1, 1)}>
                  <KeyboardArrowLeftIcon />
                </IconButton>
                <Slider
                    disabled={!this.state.allow_absolute_position}
                  step={0.1}
                  min={-1}
                  max={1}
                  valueLabelDisplay="auto"
                  onChange={(event, new_value) => this.setState({pan: new_value}, this.sendPtAbsolute)}
                  value={this.state.pan}
                />
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={this.handlePtWrapSlider("pan", 0.1, -1, 1)}>
                  <KeyboardArrowRightIcon />
                </IconButton>
            </Stack>

        )

        let zoomStep = 5;
        if (this.cam_type && this.cam_type.includes('fixed')) {
            zoomStep = 10;
        }

        let zoomSlider = (
            <Stack spacing={2} direction="column" sx={{ mb: 1 }} alignItems="center">
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={() => this.setState({zoom: Math.min(this.state.zoom + zoomStep, 100)}, this.sendZoomValue)}>
                    <ZoomInIcon />
                </IconButton>
                <Slider
                  sx={{
                    '& input[type="range"]': {
                      WebkitAppearance: 'slider-vertical',
                    },
                  }}
                  disabled={!this.state.allow_absolute_position}
                  orientation="vertical"
                  step={zoomStep}
                  aria-label="Zoom Control"
                  valueLabelDisplay="auto"
                  onKeyDown={preventHorizontalKeyboardNavigation}
                  onChange={this.handleZoomChange}
                  value={this.state.zoom}
                />
                <IconButton disabled={!this.state.allow_absolute_position} size="small" onClick={() => this.setState({zoom: Math.max(this.state.zoom - zoomStep, 0)}, this.sendZoomValue)}>
                  <ZoomOutIcon />
                </IconButton>
            </Stack>
        );

        let ptzControl = (
            <Stack direction="row" justifyContent="center" margin="auto">
                {tiltSlider}
                <Stack justifyContent="center" spacing={2}>
                    <Joystick
                        size={100}
                        baseColor="blue"
                        stickColor="red"
                        throttle={200}
                        move={this.handlePtMove}
                        stop={this.handlePtStop}
                        minDistance={40}
                    />
                    {panSlider}
                </Stack>
                {zoomSlider}

            </Stack>
        );

        let createPresetDialogue = (
            <Dialog
                open={this.state.createPresetDialogOpen}
                onClose={this.handlePresetDialogClose}
                PaperProps={{
                    component: 'form',
                    onSubmit: (event) => {
                        event.preventDefault();
                        const formData = new FormData(event.currentTarget);
                        const formJson = Object.fromEntries(formData.entries());
                        const name = formJson.name;
                        this.createPreset(name);
                        this.handlePresetDialogClose();
                    },
                }}
            >
                <DialogTitle>Preset Name</DialogTitle>
                <DialogContent>
                    <TextField
                        autoFocus
                        required
                        margin="dense"
                        id="name"
                        name="name"
                        label="Enter Preset Name"
                        type="name"
                        fullWidth
                        variant="standard"
                        inputProps={{ maxLength: 6 }}
                    />
                </DialogContent>
                <DialogActions>
                    <Button onClick={this.handlePresetDialogClose}>Cancel</Button>
                    <Button type="submit">Add</Button>
                </DialogActions>
            </Dialog>
        )

        let deletePresetDialogue = (
            <ConfirmationDialogRaw
                onClose={this.handleDeletePresetDialogClose}
                open={this.state.deletePresetDialogOpen}
                options={this.state.camPresets}
            />
        )

        let presetButtons = (
            <Stack justifyContent="center" spacing={1} margin="auto">
                {this.state.camPresets.map((preset, index) => (
                    <Button variant="outlined" key={index} aria-label="go to preset"
                            onClick={() => this.gotoPreset(preset)}>
                        {preset}
                    </Button>
                    // </ButtonGroup>
                ))}
                {this.state.camPresets.length < 5 ?
                    <Button startIcon={<AddIcon/>} onClick={this.handlePresetDialogOpen}>Add</Button>
                    : null
                }
                {this.state.camPresets.length > 0 ?
                    <Button startIcon={<DeleteIcon />} onClick={this.handleDeletePresetDialogOpen}>Delete</Button> : null
                }
                {createPresetDialogue}
                {deletePresetDialogue}
            </Stack>
        );

        let camControlButtons = (
            <Stack justifyContent="center" spacing={1} sx={{ flexDirection: { xs: "row", md: "column"} }}>
                <Button variant="outlined" color="primary">Auto-Focus</Button>
                <Button variant="outlined" color="primary">Record</Button>
            </Stack>
        )

        let liveView = (
            <div>
                <Stack spacing={2} direction="row" padding="20px">
                    <WebRPCVideoPlayer url={this.state.playerSource} />
                    {this.is_fixed_cam ? zoomSlider : null}
                </Stack>
                {this.cam_type && this.cam_type.includes('ptz') ?
                    <Box display="flex">
                        {presetButtons}
                        {ptzControl}
                    </Box> : null
                }
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

        let resetZoomButton = (
            <Button
                variant="contained"
                color="primary"
                onClick={() => this.sendControlCommand("camera_control", {"action": "reset"})}
            >
                Reset Zoom
            </Button>
        );

        let resetStream = (
            <Button
                variant="contained"
                color="primary"
                onClick={this.resetPlayerUrl}
            >
                Reset Stream
            </Button>
        );

        if (this.state.errorMessage) {
            return (
                <Box textAlign="center" padding={"20px"}>
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
                <Stack direction="row" justifyContent="center" padding={"20px"} spacing={5}>
                    {(this.state.showLiveView && this.is_fixed_cam) ? resetZoomButton : null}
                    {this.state.showLiveView ? resetStream : null}
                    {this.state.showLiveView ? disableButton : enableButton}
                </Stack>
            </Box>
        );
    }
}
