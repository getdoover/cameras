"""
Basic tests for an application.

This ensures all modules are importable and that the config is valid.
"""

def test_import_app():
    from camera_app.application import CameraApplication
    assert CameraApplication

def test_config():
    from camera_app.app_config import CameraConfig

    config = CameraConfig()
    assert isinstance(config.to_dict(), dict)

def test_ui():
    from camera_app.app_ui import CameraUI
    assert CameraUI

# def test_state():
#     from app_template.app_state import SampleState
#     assert SampleState