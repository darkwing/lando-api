# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Landing API
See the OpenAPI Specification for this API in the spec/swagger.yml file.
"""
import hmac
import logging
import os

from connexion import problem
from flask import g, request
from sqlalchemy.orm.exc import NoResultFound

from landoapi import auth
from landoapi.decorators import require_phabricator_api_key
from landoapi.models.landing import (
    InactiveDiffException,
    Landing,
    LandingNotCreatedException,
    LandingStatus,
    OverrideDiffException,
    RevisionNotFoundException,
)
from landoapi.models.patch import (
    DiffNotFoundException, DiffNotInRevisionException
)
from landoapi.validation import revision_id_to_int

logger = logging.getLogger(__name__)
TRANSPLANT_API_KEY = os.getenv('TRANSPLANT_API_KEY')


@auth.require_auth0_userinfo
@require_phabricator_api_key(optional=True)
def post(data):
    """API endpoint at POST /landings to land revision."""
    if not g.auth0_user.email:
        return problem(
            403,
            'Not Authorized',
            'You do not have a Mozilla verified email address.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403'
        )

    if not g.auth0_user.can_land_changes():
        return problem(
            403,
            'Not Authorized',
            'You do not have the required permissions to request landing.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403'
        )

    # get revision_id from body
    revision_id = revision_id_to_int(data['revision_id'])
    diff_id = data['diff_id']
    override_diff_id = data.get('force_override_of_diff_id')
    logger.info(
        {
            'path': request.path,
            'method': request.method,
            'data': data,
            'msg': 'landing requested by user'
        }, 'landing.invoke'
    )
    try:
        landing = Landing.create(
            revision_id,
            diff_id,
            g.auth0_user.email,
            g.phabricator,
            override_diff_id=override_diff_id
        )
    except RevisionNotFoundException:
        # We could not find a matching revision.
        logger.info(
            {
                'revision': revision_id,
                'msg': 'revision not found'
            }, 'landing.failure'
        )
        return problem(
            404,
            'Revision not found',
            'The requested revision does not exist',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404'
        )
    except DiffNotFoundException:
        # We could not find a matching diff
        logger.info(
            {
                'diff': diff_id,
                'msg': 'diff not found'
            }, 'landing.failure'
        )
        return problem(
            404,
            'Diff not found',
            'The requested diff does not exist',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404'
        )
    except InactiveDiffException as exc:
        # Attempt to land an inactive diff
        logger.info(
            {
                'revision': revision_id,
                'diff_id': exc.diff_id,
                'active_diff_id': exc.active_diff_id,
                'msg': 'Requested to land an inactive diff'
            }, 'landing.failure'
        )
        return problem(
            409,
            'Inactive Diff',
            'The requested diff is not the active one for this revision.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/409'
        )
    except OverrideDiffException as exc:
        # Wrong diff chosen to override.
        logger.info(
            {
                'revision': revision_id,
                'diff_id': exc.diff_id,
                'active_diff_id': exc.active_diff_id,
                'override_diff_id': exc.override_diff_id,
                'msg': 'Requested override_diff_id is not the active one'
            }, 'landing.failure'
        )
        return problem(
            409,
            'Overriding inactive diff',
            'The diff to override is not the active one for this revision.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/409'
        )
    except LandingNotCreatedException as exc:
        # We could not find a matching revision.
        logger.info(
            {
                'revision': revision_id,
                'exc': exc,
                'msg': 'error creating landing',
            }, 'landing.error'
        )
        return problem(
            502,
            'Landing not created',
            'The requested revision does exist, but landing failed.'
            'Please retry your request at a later time.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/502'
        )
    except DiffNotInRevisionException:
        # Diff's revisionID field does not equal revision_id
        logger.info(
            {
                'revision': revision_id,
                'diff_id': diff_id,
                'msg': 'Diff not it revision.',
            }, 'landing.error'
        )
        return problem(
            400,
            'Diff not related to the revision',
            'The requested diff is not related to the requested revision.',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400'
        )

    return {'id': landing.id}, 202


# TODO: SECURITY - We need to require an access_token here and ensure that
# the user is only viewing landings that are public or they own.
def get_list(revision_id=None, status=None):
    """API endpoint at GET /landings to return a list of Landing objects."""
    kwargs = {}
    if revision_id:
        kwargs['revision_id'] = revision_id_to_int(revision_id)

    if status:
        kwargs['status'] = LandingStatus(status)

    landings = Landing.query.filter_by(**kwargs).all()
    return [l.serialize() for l in landings], 200


# TODO: SECURITY - We need to require an access_token here and ensure that
# the user is only viewing a landing that is public or they own.
def get(landing_id):
    """API endpoint at /landings/{landing_id} to return stored Landing."""
    landing = Landing.query.get(landing_id)
    if not landing:
        return problem(
            404,
            'Landing not found',
            'The requested Landing does not exist',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404'
        )

    return landing.serialize(), 200


def _not_authorized_problem():
    return problem(
        403,
        'Not Authorized',
        'You\'re not authorized to proceed.',
        type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403'
    )


def update(data):
    """Update landing on pingback from Transplant.

    API-Key header is required to authenticate Transplant API

    data contains following fields:
        request_id: integer (required)
            id of the landing request in Transplant
        landed: boolean (required)
            true when operation was successful
        tree: string
            tree name as per treestatus
        rev: string
            matching phabricator revision identifier
        destination: string
            full url of destination repo
        trysyntax: string
            change will be pushed to try or empty string
        error_msg: string
            error message if landed == false
            empty string if landed == true
        result: string
            revision (sha) of push if landed == true
            empty string if landed == false
    """
    if os.getenv('PINGBACK_ENABLED', 'n') != 'y':
        logger.warning(
            {
                'data': data,
                'remote_addr': request.remote_addr,
                'msg': 'Attempt to access a disabled pingback',
            }, 'pingback.warning'
        )
        return _not_authorized_problem()

    if not hmac.compare_digest(
        request.headers.get('API-Key', ''), TRANSPLANT_API_KEY
    ):
        logger.warning(
            {
                'data': data,
                'remote_addr': request.remote_addr,
                'msg': 'Wrong API Key',
            }, 'pingback.error'
        )
        return _not_authorized_problem()

    try:
        landing = Landing.query.filter_by(request_id=data['request_id']).one()
    except NoResultFound:
        return problem(
            404,
            'Landing not found',
            'The requested Landing does not exist',
            type='https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404'
        )

    landing.update_from_transplant(
        data['landed'],
        error=data.get('error_msg', ''),
        result=data.get('result', '')
    )
    landing.save()
    return {}, 200
