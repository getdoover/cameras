import { useRef, useEffect, useState } from "react";
import videojs from 'video.js';
import 'video.js/dist/video-js.css';

export default function VideoPlayer(props) {
  const videoRef = useRef(null);
  const [player, setPlayer] = useState();

  useEffect(() => {
    // make sure Video.js player is only initialized once
    if (!player) {
      const videoElement = videoRef.current;
      if (!videoElement) return;

      setPlayer(
        videojs(videoElement, {}, () => {
          console.log("player is ready");
        })
      );
    }
  }, [videoRef]);

  useEffect(() => {
    return () => {
      if (player) {
        player.dispose();
      }
    };
  }, [player]);

  return (
    <div>
      <video className="video-js" ref={videoRef} controls>
        <source src={props.src} type="application/x-mpegURL" />
      </video>
    </div>
  );
};

