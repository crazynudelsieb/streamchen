"""streamchen — collaborative radio.

One Python package, two entry points:

* ``app.api``    — the FastAPI service that owns all business logic.
* ``app.worker`` — the playback worker that feeds Icecast.

They share the modules at this level (config, models, scheduling, …) so the
queue order a listener sees and the queue order the worker plays from are
computed by exactly the same code.
"""

# Single source of truth for the released version. The release workflow stamps
# this from the git tag it is publishing, and commits it alongside that tag, so
# a running instance always names a version that actually shipped.
__version__ = "0.1.10"
