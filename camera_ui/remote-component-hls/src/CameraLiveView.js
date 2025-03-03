import {useEffect, useState} from "react";
import ApiClient from "./api";
import React from 'react'
import {Joystick, JoystickShape} from 'react-joystick-component';
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
import Stack from '@mui/material/Stack';
import {LoadingButton} from "@mui/lab";
import ReactHlsPlayer from "react-hls-player";


const CameraLiveView = ({agentId, camName, camType, rtspServerHost, rtspServerPort, camPresets}) => {
  const [apiClient] = useState(() => new ApiClient());

  const [playerSource, setPlayerSource] = useState("");
  const [tunnel, setTunnel] = useState(null);

  const [loading, setLoading] = useState(false);
  const [showLiveView, setShowLiveView] = useState(false);

  rtspServerHost = rtspServerHost || "localhost";
  rtspServerPort = rtspServerPort || 8083;


  const gotoPreset = async (preset) => {
    await apiClient.sendControlCommand("camera_control", {"action": "goto_preset", "value": preset});
  }

  const setupTunnel = async () => {
    setLoading(true);

    let resp = await apiClient.getTunnelList(agentId, false);
    let tunnels = resp.tunnels;
    console.log(tunnels);
    let tunnel = tunnels.find(tunnel => tunnel.hostname === rtspServerHost && tunnel.port === rtspServerPort);
    if (!tunnel) {
      tunnel = await apiClient.createTunnel(agentId, {
        name: `${camName} Live View`,
        hostname: rtspServerHost,
        port: rtspServerPort,
        protocol: "http",
        is_favourite: true,
        timeout: 15,
      });
    }
    await apiClient.activateTunnel(tunnel.key);
    setTunnel(tunnel);
    // http://192.168.0.98:8083/stream/ptz_cam_1/channel/0/hls/live/index.m3u8
    setPlayerSource(`https://${tunnel.endpoint}/stream/${camName}/channel/0/hls/live/index.m3u8`);
    setTimeout(() => {
      setShowLiveView(true);
      setLoading(false);
    }, 2_000);
    return tunnel;
  };

  const teardownLiveView = async () => {
    setShowLiveView(false);
    await apiClient.deactivateTunnel(tunnel.key);
    setPlayerSource(null);
  }

  const resetPlayerUrl = () => {
      let url = playerSource;
      apiClient.sendControlCommand("camera_control", camName, {"action": "sync_ui"});
      setPlayerSource(null);
      setTimeout(() => setPlayerSource(url), 100);
  }

  let presetButtons = (
    <Stack justifyContent="center" spacing={1} margin="auto">
      {camPresets.map((preset, index) => (
        <Button variant="outlined" key={index} aria-label="go to preset"
                onClick={() => gotoPreset(preset)}>
          {preset}
        </Button>
      ))}
    </Stack>
  );

  let liveView = (
    <div>
      <Stack spacing={2} direction="row" padding="20px">
        <ReactHlsPlayer
          src={playerSource}
          autoPlay={true}
          controls={true}
          width={"100%"}
          // hlsConfig={{
          //   maxLoadingDelay: 4,
          //   minAutoBitrate: 0,
          //   lowLatencyMode: true,
          // }}
        />
      </Stack>
      {camType && camType.includes('ptz') ?
        <Box display="flex">
          {presetButtons}
        </Box> : null
      }
    </div>
  )

  let enableButton = (
    <LoadingButton
      variant="contained"
      color="secondary"
      onClick={setupTunnel}
      loading={loading}
    >
      Enable Live View
    </LoadingButton>
  );

  let disableButton = (
    <Button
      variant="contained"
      color="primary"
      onClick={teardownLiveView}
    >
      Close Live View
    </Button>
  );
  
  let resetStream = (
    <Button
      variant="contained"
      color="primary"
      onClick={resetPlayerUrl}
    >
      Reset Stream
    </Button>
  );

  // if (this.state.errorMessage) {
  //   return (
  //     <Box textAlign="center" padding={"20px"}>
  //       <Box width='fit-content' margin='4px' style={{
  //         'font': '400 1.2rem/1.5 "Roboto","Helvetica","Arial",sans-serif',
  //         color: 'rgb(0, 0 , 0, 0.54)',
  //         paddingTop: "30px",
  //         paddingBottom: "30px",
  //         margin: "auto",
  //       }}
  //       >
  //         {this.state.errorMessage}
  //       </Box>
  //       <LoadingButton
  //         variant="contained"
  //         color="primary"
  //         onClick={this.handleGetLiveView}
  //         loading={this.state.loading}
  //       >
  //         Try Again
  //       </LoadingButton>
  //     </Box>
  //   )
  // }

  return (
    <Box>
      {showLiveView ? liveView : null}
      <Stack direction="row" justifyContent="center" padding={"20px"} spacing={5}>
        {showLiveView ? resetStream : null}
        {showLiveView ? disableButton : enableButton}
      </Stack>
    </Box>
  );
}

export default CameraLiveView;