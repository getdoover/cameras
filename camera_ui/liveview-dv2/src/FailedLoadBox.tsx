import {Stack, Typography} from "@mui/material";
import ErrorIcon from "@mui/icons-material/Error";

export default function FailedLoadBox({error}: { error: string }) {
    return (
        <Stack spacing={1}
               margin={2}
               padding={1.5}
               border={"1.5px solid #00000040"}
               borderRadius={"8px"}
               direction={"row"}
               justifyContent={"center"}
        >
            <ErrorIcon color="warning"/>
            <Typography textAlign={"center"}>
                {error}
            </Typography>
        </Stack>
    );
}
