import {defineConfig} from '@rsbuild/core';
import {pluginReact} from '@rsbuild/plugin-react';
import {pluginModuleFederation} from '@module-federation/rsbuild-plugin';
import mfConfig from './module-federation.config';
import ConcatenatePlugin from './ConcatenatePlugin.ts';

export default defineConfig({
    tools: {
        rspack: {
            plugins: [
                new ConcatenatePlugin({
                    source: './dist',
                    destination: '../assets',
                    name: 'LiveViewV2.js',
                    ignore: ['main.js'], // Ignore specific files if needed
                }),
            ],
        },
    }, plugins: [
        pluginReact(),
        pluginModuleFederation(mfConfig),
    ],
    performance: {
        chunkSplit: {
            strategy: 'all-in-one', // Bundle everything into a single file
        },
    },

});