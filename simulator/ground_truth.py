"""
Instructor-only log of injected fault events (what was triggered, and
when), kept separate from the data stream the students receive. Use this
to check whether a team's monitoring/dashboard actually caught each
injected fault.
"""

import datetime


class GroundTruthLogger:
    def __init__(self, path="ground_truth.log"):
        self._file = open(path, "a", buffering=1)  # line-buffered

    def log(self, scenario: str, event: str):
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._file.write(f"{ts},{scenario},{event}\n")

    def close(self):
        self._file.close()
