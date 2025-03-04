import {useMemo, useState} from "react";
import ApiClient from "./api";
import React from 'react'
import {
  Button, Dialog,
} from '@mui/material'
import Stack from '@mui/material/Stack';
import {LoadingButton} from "@mui/lab";
import ReactHlsPlayer from "react-hls-player";
import PresetMenu from "./PresetMenu";

import {faUpRightFromSquare} from '@fortawesome/free-solid-svg-icons'
import {Box} from "@mui/system";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import GearIcon from '@mui/icons-material/Settings';
import RestartAltIcon from '@mui/icons-material/RestartAlt';

const CameraLiveView = ({
                          agentId,
                          camName,
                          camType,
                          camHostname,
                          camManagePort,
                          rtspServerHost,
                          rtspServerPort,
                          camPresets,
                          activePreset
                        }) => {
  const [apiClient] = useState(() => new ApiClient());

  const [playerSource, setPlayerSource] = useState("");
  const [tunnel, setTunnel] = useState(null);
  const [managementTunnel, setManagementTunnel] = useState(null);

  const [managementPopup, setManagementPopup] = useState(false);

  const [loading, setLoading] = useState(false);
  const [manageRedirectLoading, setManageRedirectLoading] = useState(false);
  const [showLiveView, setShowLiveView] = useState(false);

  rtspServerHost = rtspServerHost || "localhost";
  rtspServerPort = rtspServerPort || 8083;


  const gotoPreset = async (preset) => {
    await apiClient.sendControlCommand("camera_control", camName, agentId, {"action": "goto_preset", "value": preset});
  }

  const setupTunnel = async () => {
    setLoading(true);

    let resp = await apiClient.getTunnelList(agentId, false);
    let tunnels = resp.tunnels;
    let tunnel = tunnels.find(tunnel => tunnel.hostname === rtspServerHost && tunnel.port === rtspServerPort);

    // while we've fetched the tunnels, check for a management tunnel...
    let managementTunnel = tunnels.find(tunnel => tunnel.hostname === camHostname && tunnel.port === camManagePort);
    setManagementTunnel(managementTunnel);

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
    await apiClient.sendControlCommand("camera_control", camName, agentId, {"action": "sync_ui", "value": 1});
    // http://192.168.0.98:8083/stream/ptz_cam_1/channel/0/hls/live/index.m3u8
    setTimeout(() => {
      setShowLiveView(true);
      setLoading(false);
      setPlayerSource(`https://${tunnel.endpoint}/stream/${camName}/channel/0/hls/live/index.m3u8`);
    }, 3_000);

    // setTimeout(resetPlayerUrl, 5_000)
    return tunnel;
  };

  const setupManageCameraTunnel = async () => {
    setManageRedirectLoading(true);

    let tunnel = managementTunnel;
    if (!managementTunnel) {
      let tunnels = (await apiClient.getTunnelList(agentId, false)).tunnels;
      tunnel = tunnels.find(tunnel => tunnel.hostname === camHostname && tunnel.port === camManagePort);
      if (!tunnel) {
        tunnel = await apiClient.createTunnel(agentId, {
          name: `${camName} Management Page`,
          hostname: camHostname,
          port: camManagePort,
          protocol: "http",
          is_favourite: true,
          timeout: 30,
        });
      }
      setManagementTunnel(tunnel);
    }
    await apiClient.activateTunnel(tunnel.key);

    setTimeout(() => {
      setManageRedirectLoading(false);
      let res = window.open(`https://${tunnel.endpoint}`, '_blank');
      if (!res) {
        setManagementPopup(true);
      }
    }, 2_000)
  }

  const teardownLiveView = async () => {
    setShowLiveView(false);
    await apiClient.deactivateTunnel(tunnel.key);
    setPlayerSource(null);
  }

  const resetPlayerUrl = () => {
    let url = playerSource;
    apiClient.sendControlCommand("camera_control", camName, agentId, {"action": "sync_ui", "value": 1});
    setPlayerSource(null);
    setTimeout(() => setPlayerSource(url), 100);
  }

  const presetMenu = camPresets.length > 0 ?
    <PresetMenu presets={camPresets} activePreset={activePreset} onSelect={gotoPreset}/> : null;

  const liveView = useMemo(() => <ReactHlsPlayer
    src={playerSource}
    autoPlay={true}
    controls={true}
    width={"100%"}
    hlsConfig={{
      // see: https://github.com/video-dev/hls.js/issues/3077#issuecomment-704961806
      "enableWorker": true,
      "maxBufferLength": 1,
      "liveBackBufferLength": 0,
      "liveSyncDuration": 0,
      "liveMaxLatencyDuration": 5,
      "liveDurationInfinity": true,
      "highBufferWatchdogPeriod": 1,
    }}
    // hlsConfig={{
    //   maxLoadingDelay: 4,
    //   minAutoBitrate: 0,
    //   lowLatencyMode: true,
    // }}
  />, [playerSource]);

  const disableButton = (<Button variant="outlined" color="primary" onClick={teardownLiveView}>
    Close Live View
  </Button>);

  let manageCamera = (
    <Button
      loading={manageRedirectLoading}
      variant="outlined"
      onClick={setupManageCameraTunnel}
      startIcon={<GearIcon/>}
    >
      Settings
    </Button>
  );

  let resetStream = (
    <Button variant="outlined" color="primary" onClick={resetPlayerUrl} startIcon={<RestartAltIcon/>}>
      Reset Stream
    </Button>
  );

  if (!showLiveView) {
    return <Stack padding={"20px"} justifySelf={"center"} maxWidth={"sm"}>
      <LoadingButton variant="outlined" onClick={setupTunnel} loading={loading}>
        Enable Live View
      </LoadingButton>
    </Stack>
  }

  const managementPopupElem = managementPopup ? (<Dialog
    open={managementPopup}
    onClose={() => setManagementPopup(false)}
    aria-labelledby="alert-dialog-title"
    aria-describedby="alert-dialog-description"
  >
    <Box padding={5}>
      <Button endIcon={<FontAwesomeIcon icon={faUpRightFromSquare}/>} target="_blank"
              href={`https://${managementTunnel.endpoint}`} color={"primary"} variant={"outlined"}
              onClick={() => setManagementPopup(false)}
      >
        Open Camera Management Page
      </Button>
    </Box>
  </Dialog>) : null;


  return (<React.Fragment>
    {managementPopupElem}
    <Stack spacing={2} direction="column" padding="20px">
      {liveView}
      {presetMenu}
      <Stack direction="row" justifyContent="center" spacing={5}>
        {resetStream}
        {manageCamera}
        {/*{disableButton}*/}
      </Stack>
    </Stack>
  </React.Fragment>);
}

export default CameraLiveView;