/// <reference types="@rsbuild/core/types" />

/**
 * Imports the SVG file as a React component.
 * @requires [@rsbuild/plugin-svgr](https://npmjs.com/package/@rsbuild/plugin-svgr)
 */
declare module '*.svg?react' {
  import type React from 'react';
  const ReactComponent: React.FunctionComponent<React.SVGProps<SVGSVGElement>>;
  export default ReactComponent;
}

// declare module "doover_admin/dooverProvider" {
//   export const useDoover: () => any;
//   export default any;
// }

// Module federation
declare module "customer_site/hooks" {
    type ChannelIdentifier = {
        agentId: string;
        channelName: string;
    };

    export const useAgentChannel: (
        agentId: string | undefined,
        channelName: string | undefined,
    ) => any;

    export const useAgentState: (agentId: string | undefined) => any;
    export const useAgentCmds: (agentId: string | undefined) => any;

    export const useAgentSendUiCmd: (agentId: string | undefined) => any;
    export const useChannelSendMessage: (
        agentId: string | undefined,
        channelName: string | undefined,
    ) => any;
    export const useChannelMessages: (channel: ChannelIdentifier) => any;
}

declare module "customer_site/useRemoteParams" {
    export const useRemoteParams: () => any;
}

