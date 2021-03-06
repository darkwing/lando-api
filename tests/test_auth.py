# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import copy

import pytest
import requests
import requests_mock
from connexion import ProblemException
from connexion.lifecycle import ConnexionResponse
from flask import g

from landoapi.auth import (
    A0User,
    get_auth0_userinfo,
    require_access_token,
    require_auth0_userinfo,
)

from tests.auth import create_access_token, TEST_KEY_PRIV
from tests.canned_responses.auth0 import CANNED_USERINFO


def noop(*args, **kwargs):
    return ConnexionResponse(status_code=200)


def test_require_access_token_missing(app):
    with app.test_request_context('/', headers=[]):
        with pytest.raises(ProblemException) as exc_info:
            require_access_token(noop)()

    assert exc_info.value.status == 401


@pytest.mark.parametrize(
    'headers,status', [
        ([('Authorization', 'MALFORMED')], 401),
        ([('Authorization', 'MALFORMED 12345')], 401),
        ([('Authorization', 'BEARER 12345 12345')], 401),
        ([('Authorization', '')], 401),
        ([('Authorization', 'Bearer bogus')], 400),
    ]
)
def test_require_access_token_malformed(jwks, app, headers, status):
    with app.test_request_context('/', headers=headers):
        with pytest.raises(ProblemException) as exc_info:
            require_access_token(noop)()

    assert exc_info.value.status == status


def test_require_access_token_no_kid_match(jwks, app):
    key = copy.deepcopy(TEST_KEY_PRIV)
    key['kid'] = 'BOGUSKID'
    token = create_access_token(key=key)
    headers = [('Authorization', 'Bearer {}'.format(token))]

    with app.test_request_context('/', headers=headers):
        with pytest.raises(ProblemException) as exc_info:
            require_access_token(noop)()

    assert exc_info.value.status == 400
    assert exc_info.value.title == 'Authorization Header Invalid'
    assert exc_info.value.detail == (
        'Appropriate key for Authorization header could not be found'
    )


@pytest.mark.parametrize(
    'token_kwargs,status,title', [
        ({
            'exp': 1
        }, 401, 'Token Expired'),
        ({
            'iss': 'bogus issuer'
        }, 401, 'Invalid Claims'),
        ({
            'aud': 'bogus audience'
        }, 401, 'Invalid Claims'),
    ]
)
def test_require_access_token_invalid(jwks, app, token_kwargs, status, title):
    token = create_access_token(**token_kwargs)
    headers = [('Authorization', 'Bearer {}'.format(token))]

    with app.test_request_context('/', headers=headers):
        with pytest.raises(ProblemException) as exc_info:
            require_access_token(noop)()

    assert exc_info.value.status == status
    assert exc_info.value.title == title


@pytest.mark.parametrize('token_kwargs', [
    {},
])
def test_require_access_token_valid(
    jwks,
    app,
    token_kwargs,
):
    token = create_access_token(**token_kwargs)
    headers = [('Authorization', 'Bearer {}'.format(token))]
    with app.test_request_context('/', headers=headers):
        resp = require_access_token(noop)()

    assert resp.status_code == 200


def test_get_auth0_userinfo(app):
    with app.app_context():
        with requests_mock.mock() as m:
            m.get(
                '/userinfo', status_code=200, json=CANNED_USERINFO['STANDARD']
            )
            resp = get_auth0_userinfo(create_access_token())

    assert resp.status_code == 200


def test_require_auth0_userinfo_expired_token(jwks, app):
    # Make sure requiring userinfo also validates the token first.
    expired_token = create_access_token(exp=1)
    headers = [('Authorization', 'Bearer {}'.format(expired_token))]
    with app.test_request_context('/', headers=headers):
        with pytest.raises(ProblemException) as exc_info:
            require_auth0_userinfo(noop)()

    assert exc_info.value.status == 401
    assert exc_info.value.title == 'Token Expired'


@pytest.mark.parametrize(
    'exc,status,title', [
        (requests.exceptions.ConnectTimeout, 500, 'Auth0 Timeout'),
        (requests.exceptions.ReadTimeout, 500, 'Auth0 Timeout'),
        (requests.exceptions.ProxyError, 500, 'Auth0 Connection Problem'),
        (requests.exceptions.SSLError, 500, 'Auth0 Connection Problem'),
        (requests.exceptions.HTTPError, 500, 'Auth0 Response Error'),
        (requests.exceptions.RequestException, 500, 'Auth0 Error'),
    ]
)
def test_require_auth0_userinfo_auth0_request_errors(
    jwks, app, exc, status, title
):
    token = create_access_token()
    headers = [('Authorization', 'Bearer {}'.format(token))]
    with app.test_request_context('/', headers=headers):
        with requests_mock.mock() as m:
            m.get('/userinfo', exc=exc)

            with pytest.raises(ProblemException) as exc_info:
                require_auth0_userinfo(noop)()

    assert exc_info.value.status == status
    assert exc_info.value.title == title


@pytest.mark.parametrize(
    'a0status,a0kwargs,status,title', [
        (429, {'text': 'Too Many Requests'}, 429, 'Auth0 Rate Limit'),
        (401, {'text': 'Unauthorized'}, 401, 'Auth0 Userinfo Unauthorized'),
        (200, {'text': 'NOT JSON'}, 500, 'Auth0 Response Error'),
    ]
)  # yapf: disable
def test_require_auth0_userinfo_auth0_failures(
    jwks, app, a0status, a0kwargs, status, title
):
    token = create_access_token()
    headers = [('Authorization', 'Bearer {}'.format(token))]
    with app.test_request_context('/', headers=headers):
        with requests_mock.mock() as m:
            m.get('/userinfo', status_code=a0status, **a0kwargs)

            with pytest.raises(ProblemException) as exc_info:
                require_auth0_userinfo(noop)()

    assert exc_info.value.status == status
    assert exc_info.value.title == title


def test_require_auth0_userinfo_succeeded(jwks, app):
    token = create_access_token()
    headers = [('Authorization', 'Bearer {}'.format(token))]
    with app.test_request_context('/', headers=headers):
        with requests_mock.mock() as m:
            m.get(
                '/userinfo', status_code=200, json=CANNED_USERINFO['STANDARD']
            )
            resp = require_auth0_userinfo(noop)()

        assert isinstance(g.auth0_user, A0User)

    assert resp.status_code == 200


@pytest.mark.parametrize(
    'userinfo,groups,result', [
        (CANNED_USERINFO['STANDARD'], ('bogus', ), False),
        (CANNED_USERINFO['STANDARD'], ('active_scm_level_1', 'bogus'), False),
        (CANNED_USERINFO['STANDARD'], ('active_scm_level_1', ), True),
        (
            CANNED_USERINFO['STANDARD'],
            ('active_scm_level_1', 'all_scm_level_1'), True
        ),
        (CANNED_USERINFO['NO_CUSTOM_CLAIMS'], ('active_scm_level_1', ), False),
        (
            CANNED_USERINFO['NO_CUSTOM_CLAIMS'],
            ('active_scm_level_1', 'bogus'), False
        ),
        (CANNED_USERINFO['SINGLE_GROUP'], ('all_scm_level_1', ), True),
        (CANNED_USERINFO['SINGLE_GROUP'], ('active_scm_level_1', ), False),
        (
            CANNED_USERINFO['SINGLE_GROUP'],
            ('active_scm_level_1', 'all_scm_level_1'), False
        ),
        (CANNED_USERINFO['STRING_GROUP'], ('all_scm_level_1', ), True),
        (CANNED_USERINFO['STRING_GROUP'], ('active_scm_level_1', ), False),
        (
            CANNED_USERINFO['STRING_GROUP'],
            ('active_scm_level_1', 'all_scm_level_1'), False
        ),
        (CANNED_USERINFO['STANDARD'], (), True),
    ]
)
def test_user_is_in_groups(userinfo, groups, result):
    token = create_access_token()
    user = A0User(token, userinfo)
    assert user.is_in_groups(*groups) == result


@pytest.mark.parametrize(
    'userinfo,expected_email', [
        (CANNED_USERINFO['STANDARD'], 'tuser@example.com'),
        (CANNED_USERINFO['NO_EMAIL'], None),
        (CANNED_USERINFO['UNVERIFIED_EMAIL'], None),
    ]
)
def test_user_email(userinfo, expected_email):
    token = create_access_token()
    user = A0User(token, userinfo)
    assert user.email == expected_email
