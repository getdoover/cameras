import RemoteAccess from 'doover_home/RemoteAccess'
import { ThemeProvider } from '@mui/material/styles';
import React, {useState, useEffect, Component} from 'react'
import { Paper, Grid, Box, Card, Button } from '@mui/material'
// import MultiPlot from '../multiPlotWrapper'

 
export default class RemoteComponent extends RemoteAccess{
    constructor(props){
        super(props)
        this.state = {
            pending_update : {},
            agent_id: this.getUi().agent_key
        }
    }

    render() {
        
        return (
            <div>
                Hey Gents
            </div>
        );
    }
}
