import {useState} from 'react';
import {Button, InputLabel, MenuItem, Select, type SelectChangeEvent} from "@mui/material";

interface PresetMenuProps {
    presets: string[];
    activePreset: string;
    onSelect: (preset: string) => void;
}

const PresetMenu = ({presets, activePreset, onSelect}: PresetMenuProps) => {
    // const [selected, setSelected] = useState("");
    const [loading, setLoading] = useState(false);

    const handleChange = (event: SelectChangeEvent) => {
        // setSelected(event.target.value);
        setLoading(true);
        setTimeout(() => {
            setLoading(false)
        }, 6_000);
        onSelect(event.target.value);
    }

    // useEffect(() => {
    //     setSelected(activePreset || "");
    // }, [activePreset]);

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
        <>
            <InputLabel id="preset-menu-select-label">Preset Position</InputLabel>
            <Select
                variant={"outlined"}
                id="preset-menu-select"
                labelId="preset-menu-select-label"
                value={activePreset}
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
        </>
    );
}

export default PresetMenu;