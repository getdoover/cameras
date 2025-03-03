import React, {useEffect} from 'react';
import {Button, InputLabel, MenuItem, Select} from "@mui/material";

const PresetMenu = ({presets, activePreset, onSelect}) => {
  const [selected, setSelected] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const handleChange = (event) => {
    setSelected(event.target.value);
    setLoading(true);
    setTimeout(() => {setLoading(false)}, 6_000);
    onSelect(event.target.value);
  }

  useEffect(() => {
    setSelected(activePreset || "");
  }, [activePreset]);

  if (loading) {
    return <Button
      fullWidth
      loading
      loadingPosition="start"
      variant="outlined"
    >
      Loading Preset...
    </Button>

  }

  return (
    <div>
      <InputLabel id="preset-menu-select-label">Preset Position</InputLabel>
      <Select
        variant={"outlined"}
        isLoading={true}
        id="preset-menu-select"
        labelId="preset-menu-select-label"
        value={selected}
        onChange={handleChange}
        label="Preset Positions"
        fullWidth={true}
      >
        {presets.map((preset, index) => (
          <MenuItem key={index} value={preset}>
            {preset}
          </MenuItem>
        ))}
      </Select>
    </div>
  );
}

export default PresetMenu;