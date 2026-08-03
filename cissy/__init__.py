"""Cissy build server.

Turns a website into an Android app by driving the Flutter and Android
toolchains already installed on this machine.

Deliberately dependency-free: the whole thing runs with `python3 server.py` on
a freshly cloned checkout, with no venv and no pip step. Uploads use raw PUT
bodies rather than multipart forms, which is the only place that choice costs
anything and it is cheaper than the dependency.
"""

__version__ = "0.1.0"
