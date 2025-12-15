export default {
    name: 'LiveViewV2',
    remotes: {
        // feel free to change these when you're testing & deploying locally.
        // the actual values don't matter (I think?) when this is deployed.
        doover_admin: 'doover_admin@http://localhost:8080/mf-manifest.json',
        customer_site: 'customer_site@http://localhost:8025/mf-manifest.json',
    },
    exposes: {
        './LiveViewV2': './src/RemoteComponent',
    },
    shared: {
        react: {singleton: true, requiredVersion: '^18.3.1', eager: true},
        'react-dom': {singleton: true, requiredVersion: '^18.3.1', eager: true},
        'customer_site/hooks': {
            singleton: true,
            requiredVersion: false,
        },
        'customer_site/RemoteAccess': {
            singleton: true,
            requiredVersion: false,
        },
        'customer_site/queryClient': {
            singleton: true,
            requiredVersion: false,
        },
        '@refinedev/core': {
            singleton: true,
            eager: true,
            requiredVersion: false,
        },
        '@tanstack/react-query': {
            singleton: true,
            eager: true,
            requiredVersion: false,
        },

    },
};