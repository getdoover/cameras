import CameraLiveView from "./CameraLiveView";
import RemoteAccess from 'customer_site/RemoteAccess'
import RemoteComponentWrapper from 'customer_site/RemoteComponentWrapper';


class RemoteComponentInner extends RemoteAccess {
    render() {
        const ui = this.getUiState();
        const reported = ui.reported;

        // const agentId = this.props.ui_element_props?.agent_key ||
        //                this.props.ui_element_props?.ui_state?.agent_key;

        const cam_name = reported.cam_name || reported.name;
        const cam_settings = ui.desired[cam_name];
        const presets = cam_settings?.presets || [];
        const activePreset = cam_settings?.active_preset;

        // console.log("reported: ", reported, "agentId:", agentId);

        return (
            <CameraLiveView
                // agentId={agentId}
                camName={cam_name}
                camType={reported.cam_type}
                camHostname={reported.address}
                camManagePort={reported.managePort || 80}
                rtspServerHost={reported?.rtspServerHost ?? "localhost"}
                rtspServerPort={reported?.rtspServerPort ?? 8083}
                camPresets={presets}
                activePreset={activePreset}
            />
        );
    }
}

// Wrap with customer_site's providers
export default function RemoteComponent({...props}) {
    return (
        <RemoteComponentWrapper>
            <RemoteComponentInner {...props} />
        </RemoteComponentWrapper>
    );
}
