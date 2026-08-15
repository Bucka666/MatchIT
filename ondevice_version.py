"""
ondevice_version.py — single source of truth for the on-device index version.

Previously hardcoded as the literal "gs-ondevice-v1" in TWO places
(build_ondevice_index.py and app.py's /api/heartbeat) that had to be bumped
in lockstep by hand. Both now import ONDEVICE_INDEX_VERSION from here.

Bumping the version: edit ONDEVICE_INDEX_VERSION below, then redeploy.
The browser (templates/match.html) compares its cached IndexedDB manifest's
index_version against meta.json's index_version (produced from this same
constant by build_ondevice_index.py) and re-downloads on mismatch — see
GS_ONDEVICE_BASE / gsEnsureBundle in templates/match.html.
"""

ONDEVICE_INDEX_VERSION = "gs-ondevice-v2"
