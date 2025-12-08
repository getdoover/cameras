from pydoover.docker import run_app

from .application import CameraApplication
from .app_config import CameraConfig

def main():
    """
    Run the application.
    """
    run_app(CameraApplication(config=CameraConfig()))
