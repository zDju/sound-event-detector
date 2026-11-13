"""Listen to system audio and print every time a reference sound plays.

    uv run python examples/basic_detection.py path/to/reference.wav [--source app:firefox]

The reference should contain just the sound (see `sounddetector add` for trimming a longer
recording). Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import contextlib
import logging

from sound_event_detector import Detection, SoundDetector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", help="WAV file with the sound to detect")
    parser.add_argument("--source", help="default output (default), device:TEXT or app:NAME")
    parser.add_argument("--threshold", type=float, default=0.75)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    detector = SoundDetector(args.reference, source=args.source, threshold=args.threshold)

    @detector.on_detect
    def handle(event: Detection) -> None:
        print(f"{event.timestamp:%H:%M:%S}  {event.name}  similarity={event.similarity:.2f}")

    with contextlib.suppress(KeyboardInterrupt):
        detector.run()  # blocks; use detector.start() / detector.stop() to run in the background


if __name__ == "__main__":
    main()
