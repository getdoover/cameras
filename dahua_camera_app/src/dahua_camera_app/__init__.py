from pydoover.docker import run_app

from .application import DahuaCameraApplication
from .app_config import CameraConfig

def main():
    """
    Main entry point for the Dahua Camera Application.
    This function initializes and runs the application.
    """
    run_app(DahuaCameraApplication(config=CameraConfig()))
