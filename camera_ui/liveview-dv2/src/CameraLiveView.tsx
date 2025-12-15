import {useEffect, useState, useRef} from "react";
import {
    Button, Dialog, Box, Alert, Stack,
} from '@mui/material'
import PresetMenu from "./PresetMenu";

import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import GearIcon from '@mui/icons-material/Settings';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import {useCreate, useList, useCustomMutation} from "@refinedev/core";

import {useChannelSendMessage} from "customer_site/hooks";
import {useRemoteParams} from "customer_site/useRemoteParams";
import LiveViewPlayer from "./LiveViewPlayer.tsx";
import type {Tunnels} from "doover_admin/resources/tunnels";
import type {SnowflakeId} from "doover_admin/resources/types";
import {useDoover} from "doover_admin/dooverProvider";
import FailedLoadBox from "./FailedLoadBox.tsx";


enum ControlCommand {
    PowerOn = "power_on",
    GotoPreset = "goto_preset",
    SyncUI = "sync_ui",
}

interface CameraLiveViewProps {
    camName: string;
    camType: string;
    camHostname: string;
    camManagePort: number;
    rtspServerHost: string;
    rtspServerPort: number;
    camPresets: string[];
    activePreset: string;
}

const CameraLiveView = ({
                            // agentId,
                            camName,
                            // camType,
                            camHostname,
                            camManagePort,
                            rtspServerHost,
                            rtspServerPort,
                            camPresets,
                            activePreset
                        }: CameraLiveViewProps) => {
    const {agentId} = useRemoteParams();
    const sendControlMutation = useChannelSendMessage(agentId, "camera_control");
    const {site} = useDoover();

    console.log("site: ", site);

    const managementTunnelCreationAttempted = useRef(false);
    const liveViewTunnelCreationAttempted = useRef(false);

    const {result: currentTunnels, query} = useList<Tunnels>({
        resource: "tunnels",
        meta: {
            deviceId: agentId,
        },
    });

    const {mutate: createTunnelMutate} = useCreate<Tunnels>({
        resource: "tunnels",
        meta: {
            deviceId: agentId,
        },
    })

    const managementTunnel = currentTunnels.data.find(tunnel => tunnel.hostname === camHostname && tunnel.port === camManagePort);
    const liveViewTunnel = currentTunnels.data.find(tunnel => tunnel.hostname === rtspServerHost && tunnel.port === rtspServerPort);
    const tunnelsLoading = query.isLoading;
    const tunnelsError = query.isError;

    const customMutation = useCustomMutation();

    const [managementPopup, setManagementPopup] = useState<boolean>(false);

    const [loading, setLoading] = useState<boolean>(false);
    const [manageRedirectLoading, setManageRedirectLoading] = useState<boolean>(false);
    const [showLiveView, setShowLiveView] = useState<boolean>(false);

    const [countdown, setCountdown] = useState<number>();
    const [showAlert, setShowAlert] = useState<boolean>(false);

    const playerSource = useRef("");


    rtspServerHost = rtspServerHost || "localhost";
    rtspServerPort = rtspServerPort || 8083;

    useEffect(() => {
        if (liveViewTunnel) {
            playerSource.current = `https://${liveViewTunnel?.endpoint}/stream/${camName}/channel/0/hls/live/index.m3u8`
        }
    }, [liveViewTunnel, camName]);

    // create both of these tunnels if they don't exist (and everything has loaded)
    useEffect(() => {
        if (tunnelsLoading || tunnelsError || managementTunnelCreationAttempted.current || managementTunnel) return;

        console.log("Creating management tunnel...");
        managementTunnelCreationAttempted.current = true;

        createTunnelMutate({
            values: {
                name: `${camName} Management Page`,
                hostname: camHostname,
                port: camManagePort,
                protocol: "http",
                is_favourite: true,
                timeout: 30,
            },
        });
    }, [tunnelsError, tunnelsLoading, managementTunnel]);

    useEffect(() => {
        if (tunnelsLoading || tunnelsError || liveViewTunnelCreationAttempted.current || liveViewTunnel) return;

        console.log("Creating Live Veiw tunnel...")
        createTunnelMutate({
            values: {
                name: `${camName} Live View`,
                hostname: rtspServerHost,
                port: rtspServerPort,
                protocol: "http",
                is_favourite: true,
                timeout: 15,
            },
        });
    }, [tunnelsLoading, tunnelsError, liveViewTunnel])

    const activateTunnel = async (tunnel_id: SnowflakeId) => {
        await customMutation.mutateAsync(
            {
                url: `/tunnels/${tunnel_id}/activate`,
                method: "post",
                values: {},
                meta: {organisation: site?.id}
            })
    }

    // const deactivateTunnel = async (tunnel_id: SnowflakeId) => {
    //     await customMutation.mutateAsync(
    //         {url: `/tunnels/${tunnel_id}/deactivate`, method: "post", values: {}})
    // }

    const sendControlCommand = async (action: ControlCommand, value: string | number, options?: Parameters<typeof sendControlMutation.mutate>[1]) => {
        await sendControlMutation.mutateAsync({
            [camName]: {
                "action": action,
                "value": value,
                "task_id": window.crypto.randomUUID()
            }
        }, options)
    }


    const gotoPreset = async (preset: string) => {
        await sendControlCommand(ControlCommand.PowerOn, 1);
        await sendControlCommand(ControlCommand.GotoPreset, preset);
    }

    const setupTunnel = async () => {
        setLoading(true);
        setCountdown(90);
        setShowAlert(true);

        let countdownInterval: NodeJS.Timeout;

        const startCountdown = () => {
            countdownInterval = setInterval(() => {
                setCountdown(prev => {
                    if (prev === undefined || prev <= 5) {
                        clearInterval(countdownInterval);
                        setShowAlert(false);
                        return 0;
                    }
                    return prev ? prev - 5 : 0;
                });
            }, 5000);
        };

        startCountdown();

        await activateTunnel(liveViewTunnel.id);
        await sendControlCommand(ControlCommand.PowerOn, 1);
        await sendControlCommand(ControlCommand.SyncUI, 1);

        // http://192.168.0.98:8083/stream/ptz_cam_1/channel/0/hls/live/index.m3u8
        setTimeout(() => {
            setShowLiveView(true);
            setLoading(false);
        }, 1_000);

        // setTimeout(resetPlayerUrl, 5_000)
    };

    const setupManageCameraTunnel = async () => {
        setManageRedirectLoading(true);
        await sendControlCommand(ControlCommand.PowerOn, 1);
        await activateTunnel(managementTunnel.id);

        setTimeout(() => {
            setManageRedirectLoading(false);
            // window.open will return none if the popup is blocked
            if (!window.open(`https://${managementTunnel.endpoint}`, '_blank')) {
                setManagementPopup(true);
            }
        }, 2_000)
    }

    // const teardownLiveView = async () => {
    //     setShowLiveView(false);
    //     await deactivateTunnel(tunnel.id);
    //     setPlayerSource("");
    // }

    const resetPlayerUrl = async () => {
        const baseUrl = playerSource.current.split("?")[0];
        const refreshedUrl = `${baseUrl}?t=${Date.now()}`;

        await sendControlCommand(ControlCommand.PowerOn, 1);
        await sendControlCommand(ControlCommand.SyncUI, 1);

        playerSource.current = refreshedUrl;
    };

    const onPlayerReady = () => {
        setCountdown(0);
        setShowAlert(false);
    }

    if (tunnelsError) {
        return <FailedLoadBox error={"Failed to load tunnels"}/>
    }

    if (tunnelsLoading) {
        return <FailedLoadBox error={"Loading tunnels..."}/>
    }

    if (!managementTunnel || !liveViewTunnel) {
        return <FailedLoadBox error={"Setting up camera tunnels. Please wait..."}/>
    }

    const presetMenu = camPresets.length > 0 ?
        <PresetMenu presets={camPresets} activePreset={activePreset} onSelect={gotoPreset}/> : null;


    // const disableButton = (<Button variant="outlined" color="primary" onClick={teardownLiveView}>
    //     Close Live View
    // </Button>);

    const manageCamera = (<Button
        loading={manageRedirectLoading}
        variant="outlined"
        onClick={setupManageCameraTunnel}
        startIcon={<GearIcon/>}
    >
        Settings
    </Button>);

    const resetStream = (
        <Button variant="outlined" color="primary" onClick={resetPlayerUrl} startIcon={<RestartAltIcon/>}>
            Reset Stream
        </Button>);

    if (!showLiveView) {
        return <Stack padding={"20px"} justifySelf={"center"} maxWidth={"sm"}>
            <Button variant="outlined" onClick={setupTunnel} loading={loading}>
                Enable Live View
            </Button>
        </Stack>
    }

    const managementPopupElem = managementPopup ? (<Dialog
        open={managementPopup}
        onClose={() => setManagementPopup(false)}
        aria-labelledby="alert-dialog-title"
        aria-describedby="alert-dialog-description"
    >
        <Box padding={5}>
            <Button endIcon={<OpenInNewIcon/>} target="_blank"
                    href={`https://${managementTunnel.endpoint}`} color={"primary"} variant={"outlined"}
                    onClick={() => setManagementPopup(false)}
            >
                Open Camera Management Page
            </Button>
        </Box>
    </Dialog>) : null;

    console.log("player source: ", playerSource);


    return (<>
        {managementPopupElem}
        <Stack spacing={2} direction="column" padding="20px">
            {showAlert && countdown !== null && (<Alert severity="info">
                Camera may take {countdown} second{countdown !== 1 ? 's' : ''} to awaken from sleep
            </Alert>)}
            <LiveViewPlayer source={playerSource.current} onReady={onPlayerReady}/>
            {presetMenu}
            <Stack direction="row" justifyContent="center" spacing={5}>
                {resetStream}
                {manageCamera}
                {/*{disableButton}*/}
            </Stack>
        </Stack>
    </>);
}

export default CameraLiveView;