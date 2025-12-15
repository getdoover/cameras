import {useCallback, useRef} from "react";
import ReactPlayer from "react-player";

const LiveViewPlayer = ({source, onReady}: { source: string, onReady?: () => void }) => {
    const playerRef = useRef<HTMLVideoElement | null>(null);

    const setPlayerRef = useCallback((player: HTMLVideoElement) => {
        if (!player) return;
        playerRef.current = player;
        console.log(player);
    }, []);

    // hls.on(hls.constructor.Events.MANIFEST_PARSED, () => {
    //     setCountdown(null);
    //     setShowAlert(false);
    //     console.log("HLS manifest parsed, ready to play.");
    // });

    return <ReactPlayer
        key={source}
        ref={setPlayerRef}
        src={source}
        autoPlay
        controls
        width={"100%"}
        height={"100%"}
        onReady={() => {
            console.log("ReactPlayer Ready");
            onReady?.()
        }}
        onError={(e) => console.error("ReactPlayer Error:", e)}
        config={{
            hls: {
                // see: https://github.com/video-dev/hls.js/issues/3077#issuecomment-704961806
                "enableWorker": true,
                "maxBufferLength": 1,
                "lowLatencyMode": true,
            }
        }}
    />


}

export default LiveViewPlayer;