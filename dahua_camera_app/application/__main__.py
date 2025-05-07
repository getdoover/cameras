from pydoover.docker import run_app

from .application import DahuaCameraApplication
from .app_config import CameraConfig


if __name__ == "__main__":
    run_app(DahuaCameraApplication(config=CameraConfig()))
