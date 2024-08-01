import {useEffect, useRef, useState} from "react";
import RTSPtoWEBPlayer from "./rtsp-to-web-player";

const ReactPlayer = ({url}) => {

    const playerElement = useRef(null);
    const [player, setPlayer] = useState(null);

    useEffect(() => {
        if (player === null) {
            setPlayer(new RTSPtoWEBPlayer({
                parentElement: playerElement.current, controls: true
            }));
        }
        // else if (player.video.onpause === null && player.video.onplay === null) {
        //     player.video.onpause = stateListener;
        //     player.video.onplay = stateListener;
        // }
        if (url !== null && player !== null) {
            player.load(url);
        }

        return () => {
            if (player !== null) {
                player.destroy();
            }
        }
    }, [url, player]);

    return <div ref={playerElement}/>;
}

export default ReactPlayer;