//const HtmlWebpackPlugin = require('html-webpack-plugin');
const ModuleFederationPlugin = require('webpack').container.ModuleFederationPlugin;
const path = require('path');
const ExternalTemplateRemotesPlugin = require('external-remotes-plugin');
const ConcatenatePlugin = require('./ConcatPlugin');


module.exports = {
  entry: './src/RemoteComponent.js',
  mode: 'development',
  // mode: 'production',

  target: 'web',

  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: 'main.js',
    },
  module: {
    rules: [
      {
        test: /\.jsx?$/,
        loader: 'babel-loader',
        exclude: /node_modules/,
        options: {
          presets: [
              "@babel/preset-env",
           ["@babel/preset-react", {"runtime": "automatic"}]
        ],
        },
      },
      {
        test: /\.css$/i,
        use: ['style-loader','css-loader'],
      },
      {
        test: /\.svg$/i,
        type: 'asset/resource',
      },
    ],
  },
  plugins: [
    new ModuleFederationPlugin({
      name: 'DahuaCameraUi',
      filename: 'DahuaCameraUi.js',
      remotes: {
        doover_home : 'doover_home@[window.dooverRemoteAccess_remoteUrl]'
      },
      exposes: {
        './DahuaCameraUi': './src/RemoteComponent',
      },
      // adds react as shared module
      // version is inferred from package.json
      // there is no version check for the required version
      // so it will always use the higher version found
      shared: {
        react: {
          import: 'react', // the "react" package will be used a provided and fallback module
          shareKey: 'react', // under this name the shared module will be placed in the share scope
          shareScope: 'default', // share scope with this name will be used
          singleton: true, // only a single version of the shared module is allowed
        },
        './node_modules/react-dom': {
          singleton: true, // only a single version of the shared module is allowed
        },
      },
    }),
    new ConcatenatePlugin({
      source: "./dist",
      destination: "../assets",
      name: 'DahuaCameraUi.js',
      ignore: 'main.js'
    }),
    new ExternalTemplateRemotesPlugin(),

// new HtmlWebpackPlugin({
// template: './public/index.html',
// }),

],
};
