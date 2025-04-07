Notes for future me:

- `deployment` directory is because the whole app directory is 70kB or so and too big for deployments
- `camera_iface.py` should probably be merged with `application.py`, or rejigged somewhat. It's from how camera_iface worked in the past with specifying multiple cameras and only one instance of the container.
- `None` for a power pin currently spits out an error message - although it doesn't matter.