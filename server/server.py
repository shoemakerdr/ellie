import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from itertools import *
from operator import *
from typing import (Any, Dict, Iterator, List, NamedTuple, Optional, Tuple,
                    TypeVar)
from urllib.parse import urlparse

import boto3
import botocore
from flask import (Flask, jsonify, redirect, render_template, request, session,
                   url_for)
from opbeat.contrib.flask import Opbeat
from werkzeug.routing import BaseConverter, HTTPException, ValidationError

from . import assets, constants, repository
from .data.api_error import ApiError
from .data.package import Package
from .data.package_name import PackageName
from .data.project_id import ProjectId
from .data.revision_id import RevisionId
from .data.version import Version

app = Flask(__name__)
app.secret_key = constants.COOKIE_SECRET
app.config.update(
    SESSION_COOKIE_SECURE=constants.PRODUCTION,
    PERMANENT_SESSION_LIFETIME=constants.SESSION_LIFETIME
)

if constants.PRODUCTION:
    opbeat = Opbeat(
        app,
        organization_id=os.environ['OPBEAT_ORGANIZATION_ID'],
        app_id=os.environ['OPBEAT_APP_ID'],
        secret_token=os.environ['OPBEAT_SECRET_TOKEN']
    )


class ProjectIdConverter(BaseConverter):
    def to_python(self, value: str) -> Optional[ProjectId]:
        return ProjectId.from_string(value)

    def to_url(self, value: ProjectId) -> str:
        return str(value)


class VersionConverter(BaseConverter):
    def to_python(self, value: str) -> Optional[Version]:
        return Version.from_string(value)

    def to_url(self, value: Version) -> str:
        return str(value)


app.url_map.converters['project_id'] = ProjectIdConverter
app.url_map.converters['version'] = VersionConverter


@app.errorhandler(ApiError)
def handle_error(error: ApiError) -> Any:
    response = jsonify({'status': error.status_code, 'message': error.message})
    response.status_code = error.status_code
    return response


DEFAULT_ERROR_MESSAGE = "There was a problem with the server"


@app.errorhandler(Exception)
def handle_default_error(error: Exception) -> Any:
    traceback.print_exc()

    if request.path.startswith('/api'):
        code = getattr(error, "code", 500)
        description = DEFAULT_ERROR_MESSAGE
        if code != 500:
            description = getattr(error, "description", DEFAULT_ERROR_MESSAGE)
        response = jsonify({'status': code, 'message': description})
        response.status_code = code
        return response
    raise error


def parse_int(string: str) -> Optional[int]:
    try:
        return int(string)
    except:
        return None


@app.route('/api/terms/<int(min=0):terms_version>/accept', methods=['POST'])
def accept_terms(terms_version: int) -> Any:
    repository.accept_terms(terms_version)
    return jsonify({})


@app.route('/api/packages/<version:elm_version>/<string:user>/<string:project>/versions')
def tags(elm_version: Version, user: str, project: str) -> Any:
    package_name = PackageName(user, project)
    versions = repository.get_versions_for_elm_version_and_package(
        elm_version,
        package_name
    )

    if len(versions) == 0:
        raise ApiError(404, 'Package not found')

    return jsonify([v.to_json() for v in versions])


@app.route('/api/search')
def search() -> Any:
    query = request.args.get('query')
    if not isinstance(query, str):
        raise (ApiError(400, 'query field must be a string'))

    elm_version = request.args.get('elmVersion')
    if not isinstance(elm_version, str):
        raise ApiError(400, 'elm version must be a semver string like 0.18.0')

    parsed_elm_version = Version.from_string(elm_version)
    if parsed_elm_version is None:
        raise ApiError(400, 'elm version must be a semver string like 0.18.0')

    packages = repository.search(parsed_elm_version, query)

    return jsonify([p.to_json() for p in packages])


@app.route('/api/upload/existing')
def get_existing_upload_urls() -> Any:
    project_id_string = request.args.get('projectId')
    if project_id_string is None:
        raise ApiError(400, 'Required parameter `projectId` was not provided')
    project_id = ProjectId.from_string(project_id_string)
    if project_id is None:
        raise ApiError(400, 'Unparseable `projectId` parameter')

    revision_number_string = request.args.get('revisionNumber')
    if revision_number_string is None:
        raise ApiError(
            400, 'Required parameter `revisionNumber` was not provided')
    revision_number = parse_int(revision_number_string)
    if revision_number is None:
        raise ApiError(400, 'Unparseable `revisionNumber` must be an integer')
    if revision_number < 1:
        raise ApiError(400, 'Parameter `revisionNumber` must be positive')

    expected_revision_id = RevisionId(project_id, revision_number - 1)
    if not repository.revision_exists(expected_revision_id):
        raise ApiError(400, 'Revision with ID `' +
                       str(expected_revision_id) + '` does not exist to be updated')

    response = jsonify({
        'revision': repository.get_revision_upload_signature(expected_revision_id),
        'result': repository.get_result_upload_signature(expected_revision_id)
    })

    return response


@app.route('/api/revisions/default')
def get_default_revision() -> Any:
    result = repository.get_latest_defaults(Version(0, 18, 0))
    if result is None:
        raise ApiError(500, 'Could not load default packages')
    (default_core, default_html) = result
    return jsonify({
        'packages': [default_core.to_json(), default_html.to_json()],
        'elmVersion': '0.18.0',
        'title': '',
        'description': '',
        'id': None,
        'elmCode': '''module Main exposing (main)

import Html exposing (Html, text)


main : Html msg
main =
    text "Hello, World!"
''',
        'htmlCode': '''<html>
<head>
  <style>
    /* you can style your program here */
  </style>
</head>
<body>
  <script>
    var app = Elm.Main.fullscreen()
    // you can use ports and stuff here
  </script>
</body>
</html>
'''
    })


def remove_ansi_colors(input: str) -> str:
    return re.sub(r'(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]', '', input)


@app.route('/api/format', methods=['POST'])
def format() -> Any:
    data: Dict[str, Any] = request.get_json()
    maybe_source: Optional[str] = data['source']

    if maybe_source is None:
        raise ApiError(
            400, 'source attribute is missing, source must be a string')

    elm_format_path = os.path.realpath(
        os.path.dirname(os.path.realpath(__file__)) +
        '/../node_modules/.bin/elm-format')
    process_output = subprocess.run(
        [elm_format_path, '--stdin'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=maybe_source.encode('utf-8'))

    if process_output.returncode != 0:
        stderr_as_str = process_output.stderr.decode('utf-8')
        cleaned_error = remove_ansi_colors(
            '\n'.join(stderr_as_str.split('\n')[1:]))
        raise ApiError(400, cleaned_error)

    return jsonify({'result': process_output.stdout.decode('utf-8')})


EDITOR_CONSTANTS = {
    'ENV': os.environ['ENV'],
    'APP_JS': assets.asset_path('editor.js'),
    'APP_CSS': assets.asset_path('editor.css'),
    'GTM_ID': os.environ['GTM_ID'],
    'PROFILE_PIC': 'idk.jpg',
    'CDN_BASE': os.environ['CDN_BASE'],
    'SERVER_HOSTNAME': os.environ['SERVER_HOSTNAME'],
    'LATEST_TERMS_VERSION': constants.LATEST_TERMS_VERSION
}


@app.route('/')
@app.route('/new')
def new() -> Any:
    data = {
        'accepted_terms_version': session.get('v1', {}).get('accepted_terms_version')
    }

    return render_template('new.html', constants=EDITOR_CONSTANTS, data=data)


@app.route('/<project_id:project_id>/<int(min=0):revision_number>')
def existing(project_id: ProjectId, revision_number: int) -> Any:
    if project_id.is_old:
        url = url_for('existing', project_id=project_id,
                      revision_number=revision_number)
        return redirect(url, code=301)

    revision_id = RevisionId(project_id, revision_number)
    revision = repository.get_revision(revision_id)
    if revision is None:
        return redirect('new', code=303)

    data = {
        'accepted_terms_version': repository.get_accepted_terms_version(),
        'title': revision.title,
        'description': revision.description,
        'url': EDITOR_CONSTANTS['SERVER_HOSTNAME'] + '/' + str(project_id) + '/' + str(revision_number)
    }

    return render_template('existing.html', constants=EDITOR_CONSTANTS, data=data)


EMBED_CONSTANTS = {
    'ENV': os.environ['ENV'],
    'APP_JS': assets.asset_path('embed.js'),
    'APP_CSS': assets.asset_path('embed.css'),
    'GTM_ID': os.environ['GTM_ID'],
    'PROFILE_PIC': 'idk.jpg',
    'CDN_BASE': os.environ['CDN_BASE'],
    'SERVER_HOSTNAME': os.environ['SERVER_HOSTNAME'],
    'LATEST_TERMS_VERSION': constants.LATEST_TERMS_VERSION
}


@app.route('/a/terms/<int(min=1):terms_version>')
def terms(terms_version: int) -> Any:
    return render_template('terms/' + str(terms_version) + '.html')


@app.route('/embed/<project_id:project_id>/<int(min=0):revision_number>')
def embed(project_id: ProjectId, revision_number: int) -> Any:
    if project_id.is_old:
        url = url_for('embed', project_id=project_id,
                      revision_number=revision_number)
        return redirect(url, code=301)

    data = {}

    revision_id = RevisionId(project_id, revision_number)
    revision = repository.get_revision(revision_id)
    if revision is not None:
        data['title'] = revision.title
        data['description'] = revision.description
        data['url'] = EMBED_CONSTANTS['SERVER_HOSTNAME'] + \
            '/embed/' + str(project_id) + '/' + str(revision_number)

    return render_template('embed.html', constants=EMBED_CONSTANTS, data=data)


@app.route('/oembed')
def oembed() -> Any:
    url = request.args.get('url')
    width_param = parse_int(request.args.get('width'))
    height_param = parse_int(request.args.get('height'))

    width = width_param if width_param is not None else 800
    height = height_param if height_param is not None else 400

    parsed_url = urlparse(url)
    if not parsed_url.hostname or 'ellie-app.com' not in parsed_url.hostname or not parsed_url.path:
        raise ApiError(404, 'revision not found')

    split = parsed_url.path.split('/')
    if len(split) != 3:
        raise ApiError(404, 'revision not found')

    [_, project_id_str, revision_number_str] = split

    project_id = ProjectId.from_string(project_id_str)
    if project_id is None:
        raise ApiError(404, 'revision not found')

    revision_number = parse_int(revision_number_str)
    if revision_number is None:
        raise ApiError(404, 'revision not found')

    revision_id = RevisionId(project_id, revision_number)
    revision = repository.get_revision(revision_id)
    if revision is None:
        raise ApiError(404, 'revision not found')

    return jsonify({
        'width': 'width',
        'height': 'height',
        'type': 'rich',
        'version': '1.0',
        'title': revision.title,
        'provider_name': 'ellie-app.com',
        'provider_url': 'https://ellie-app.com',
        'html': '<iframe src="' + EDITOR_CONSTANTS['SERVER_HOSTNAME'] + '/embed/' + str(project_id) + '/' + str(revision_number) + '" width=' + str(width) + ' height=' + str(height) + ' frameBorder="0" allowtransparency="true"></iframe>'
    })
