#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""The /api/residues endpoint the picker reads, as a Flask blueprint.

INSTALLING
----------
Drop this beside predPEP.py and register it once, next to wherever the other
routes are set up:

    from residue_routes import residues_bp
    app.register_blueprint(residues_bp)

A blueprint rather than an edit inside predPEP.py so the catalogue, its
validation and its route stay in one reviewable unit, and so this can be
removed again by deleting one line.

AUTHENTICATION
--------------
The route is read-only and returns nothing a submitter could not learn by
reading the params directory, so it is not token-gated by default. If every
endpoint on this app is expected to carry the API-token check, wrap the view
with the same decorator the other routes use -- see REQUIRE_AUTH below.

CACHING
-------
The catalogue is read from disk once and held, because it is the same for
every request and changes only when params are regenerated. That means a
regeneration needs either a service restart or a POST to the companion
/api/residues/reload route. The cache is deliberate: a 282-row CSV plus two
directory listings per page load is waste, and silently re-reading on every
request would hide the fact that the catalogue has a lifecycle at all.
"""

import os
import threading

from flask import Blueprint, jsonify

import residue_catalogue as rc

residues_bp = Blueprint('residues', __name__)

# Set to a decorator (e.g. the app's require_token) to gate the route.
REQUIRE_AUTH = None

_lock = threading.Lock()
_cache = None


def get_catalogue(force=False):
    """The process-wide catalogue, read from disk at most once unless forced."""
    global _cache
    with _lock:
        if _cache is None or force:
            _cache = rc.load_catalogue()
        return _cache


def _payload():
    cat = get_catalogue()
    # Only what the picker renders. params_dir is a server path and has no
    # business in a browser response.
    return {
        'canonical': cat['canonical'],
        'noncanonical': cat['noncanonical'],
        'groups': cat['groups'],
        'counts': cat.get('counts', {'canonical': 0, 'noncanonical': 0,
                                     'with_rotlib': 0, 'without_rotlib': 0}),
        'canonical_editable': cat['canonical_editable'],
        'canonical_note': cat['canonical_note'],
        'error': cat['error'],
    }


@residues_bp.route('/api/residues', methods=['GET'])
def residues():
    return jsonify(_payload())


@residues_bp.route('/api/residues/reload', methods=['POST'])
def residues_reload():
    """Re-read the catalogue after params have been regenerated."""
    get_catalogue(force=True)
    c = get_catalogue()
    return jsonify({'ok': c['error'] is None, 'error': c['error'],
                    'counts': c.get('counts', {})})


if REQUIRE_AUTH is not None:                    # pragma: no cover
    residues.__dict__.setdefault('_wrapped', True)
    residues_bp.view_functions['residues.residues'] = REQUIRE_AUTH(residues)
    residues_bp.view_functions['residues.residues_reload'] = \
        REQUIRE_AUTH(residues_reload)


# ---------------------------------------------------------------------------
# For predPEP.py's own submission handler. Call this where the rest of the
# design spec is validated, and keep BOTH return values: the rejected list is
# the error message, and discarding it turns a typo into a silently smaller
# design palette.
#
#     from residue_routes import validate_design_residues
#     codes, bad = validate_design_residues(spec.get('design_residues'))
#     if bad:
#         return error(400, '; '.join('%s: %s' % (b['value'], b['reason'])
#                                     for b in bad))
#
# Then, when writing the job's flags, hand `codes` to
# residue_catalogue.write_palette_file() and pass that file as
# -packing:packer_palette:extra_base_type_file.
# ---------------------------------------------------------------------------
def validate_design_residues(selection):
    return rc.validate_selection(selection, get_catalogue())
