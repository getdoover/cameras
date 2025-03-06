import RemoteAccess from 'doover_home/RemoteAccess'
import React from 'react'
import CameraLiveView from "./CameraLiveView";

export default class RemoteComponent extends RemoteAccess {
  render() {
    const agent_id = this.getUi().agent_key;
    const ui = this.getUiState();
    const reported = ui.reported;
    // console.log(reported);
    return <CameraLiveView
      agentId={agent_id}
      camName={reported.cam_name || reported.name}
      camType={reported.cam_type}
      camHostname={reported.address}
      camManagePort={reported.managePort || 80}
      rtspServerHost={reported?.rtspServerHost ?? "localhost"}
      rtspServerPort={reported?.rtspServerPort ?? 8083}
      camPresets={reported?.presets || []}
      activePreset={reported.active_preset}
    />
  }
}
