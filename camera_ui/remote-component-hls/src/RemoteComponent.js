import RemoteAccess from 'doover_home/RemoteAccess'
import React from 'react'
import CameraLiveView from "./CameraLiveView";

export default class RemoteComponent extends RemoteAccess {
  render() {
    const agent_id = this.getUi().agent_key;
    const ui = this.getUiState();
    const reported = ui.reported;
    //
    // console.log("syncing ui...")
    // const state = this.getUiState();
    // const reported = state.reported;
    // console.log(state);
    // this.cam_base = reported.address + ":" + reported.port;
    // this.cam_name = reported.name;
    // this.rtsp_uri = reported.rtsp_uri;
    // this.cam_type = reported.cam_type;
    // this.is_fixed_cam = this.cam_type && this.cam_type.includes('fixed');
    // let presets = reported?.presets || [];
    // let position = reported?.cam_position;
    // console.log(position?.x, position?.y, position?.zoom)
    // let allow = reported?.allow_absolute_position;
    // if (allow === undefined) {
    //   allow = this.state.allow_absolute_position;
    // }

    return <CameraLiveView
      agentId={agent_id}
      camName={reported.name}
      camType={reported.cam_type}
      rtspServerHost={reported.rtspServerHost}
      rtspServerPort={reported.rtspServerPort}
      camPresets={reported?.presets || []}
    />
  }
}
