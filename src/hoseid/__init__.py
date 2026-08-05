"""hoseid -- trail-camera capture identification.

Layered so the expensive parts are throwaway and the irreplaceable parts are protected:

    landing/   append-only, immutable capture archive + sidecars
    derived/   detections, crops, rollups -- delete and rebuild at will
    tags/      human labels, the only irreplaceable data in the system

The design invariants these layers encode are documented in docs/INVARIANTS.md. They are not
style preferences; violating one is a design regression.
"""
__version__ = "0.1.0"
