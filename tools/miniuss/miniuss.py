"""A simulated USS exposing TCL4 endpoints.

Copyright 2018 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import datetime
import logging
import os
import re
import sys
import threading

import flask
import jwt
import requests
from rest_framework import status

import config
import formatting
import interuss_platform

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)
log = logging.getLogger('MiniUss')
log.setLevel(logging.DEBUG)
webapp = flask.Flask(__name__)  # Global object serving the API


# interuss_platform.Client managing communication with InterUSS Platform grid.
grid_client = None

# operations for this USS by GUFI.
operations = {}

# UVRs for this USS by ID.
uvrs = {}

# Public key for validating access tokens at USS endpoints.
public_key = None

# Content that must be in Authorization header to use control endpoints.
control_authorization = None

# Bodies of notifications so they can be viewed later.
notification_lock = threading.Lock()
notification_logs = {}

# Slippy cells in which to always listen for notifications.
always_listen = []

# Bounds for operator entry beyond operations' bounds.
min_listen_time = None
max_listen_time = None


def _update_operations(new_gufi=None):
  """Replace Operator entry in grid with current set of Operations."""
  try:
    grid_client.upsert_operator(operations.values(), min_listen_time, max_listen_time)
  except requests.HTTPError as e:
    msg = ('Error updating InterUSS Platform Operator entry: ' +
           e.response.content)
    flask.abort(e.response.status_code, msg)

  for slippy_cell in always_listen:
    try:
      grid_client.insert_observer(slippy_cell, min_listen_time, max_listen_time)
    except requests.HTTPError as e:
      msg = 'Error inserting observer Operator into cell %s: %s' % (slippy_cell, e.response.content)
      flask.abort(e.response.status_code, msg)


def _error(status_code, content):
  log.error('%d: %s', status_code, content)
  return content, status_code


def _log_request_body(key, source, action=None):
  entry = {
    'received': datetime.datetime.now().isoformat(),
    'source': source,
    'content': flask.request.json,
    'uss': flask.request.json.get('uss_name', '???')
  }
  if action:
    entry['action'] = action
  with notification_lock:
    history = notification_logs.get(key, [])
    history.append(entry)
    notification_logs[key] = history
  return entry


def _string_to_bool(s):
  return s.lower() in {'true', 't', 'y', '1', 'yes'}


# == Control and status endpoints ==

@webapp.route('/', methods=['GET'])
@webapp.route('/status', methods=['GET'])
def status_endpoint():
  log.debug('Status requested')
  return flask.jsonify({
    'status': 'success',
    'operations': operations.keys(),
    'uss_baseurl': grid_client.uss_baseurl})


@webapp.route('/client/alwayslisten', methods=['GET', 'PUT'])
def alwayslisten_endpoint():
  global always_listen
  if flask.request.method == 'GET':
    return flask.jsonify(always_listen)
  elif flask.request.method == 'PUT':
    _validate_control()
    cells = flask.request.json
    for slippy_cell in cells:
      if not re.match(r'\d+/\d+/\d+', slippy_cell):
        return _error(status.HTTP_400_BAD_REQUEST, 'Invalid slippy cell: %s' % slippy_cell)
    always_listen = cells
    _update_operations()
    return '', status.HTTP_204_NO_CONTENT
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)


@webapp.route('/client/operator_entries', methods=['DELETE'])
def operator_entries_endpoint():
  _validate_control()
  cells = flask.request.json
  result = {}
  for slippy_cell in cells:
    if not re.match(r'\d+/\d+/\d+', slippy_cell):
      return _error(status.HTTP_400_BAD_REQUEST, 'Invalid slippy cell: %s' % slippy_cell)
    response = grid_client.remove_operator_by_cell(slippy_cell)
    result[slippy_cell] = {'code': response.status_code,
                           'content': response.json() if response.status_code == 200 else response.content}
  return flask.jsonify(result)


@webapp.route('/client/operation/<gufi>', methods=['GET', 'PUT', 'DELETE'])
def client_operation_endpoint(gufi):
  if flask.request.method == 'GET':
    if gufi in operations:
      return flask.jsonify(operations[gufi])
    else:
      flask.abort(status.HTTP_404_NOT_FOUND)
  elif flask.request.method == 'PUT':
    log.debug('Operation upsert requested')
    _validate_control()
    operation = flask.request.json
    operations[operation['gufi']] = operation
    _update_operations(operation['gufi'])
    return flask.jsonify(operation)
  elif flask.request.method == 'DELETE':
    log.debug('Operation deletion requested: %s', gufi)
    _validate_control()
    if gufi in operations:
      del operations[gufi]
    else:
      return _error(status.HTTP_404_NOT_FOUND, 'GUFI %s not found' % gufi)
    _update_operations(gufi)
    return '', status.HTTP_204_NO_CONTENT
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)


@webapp.route('/client/uvr/<message_id>', methods=['GET', 'PUT', 'DELETE'])
def client_uvr_endpoint(message_id):
  global uvrs
  if flask.request.method == 'GET':
    if message_id in uvrs:
      return flask.jsonify(uvrs[message_id])
    else:
      flask.abort(status.HTTP_404_NOT_FOUND)
  elif flask.request.method == 'PUT':
    log.debug('UVR upsert requested')
    _validate_control()

    # Emplace UVR in InterUSS Platform grid
    uvr = flask.request.json
    try:
      grid_contents = grid_client.upsert_uvr(uvr)
    except requests.HTTPError as e:
      return _error(e.response.status_code, str(e))
    uvr = [u for u in grid_contents['data']['uvrs'] if u['message_id'] == message_id][0]
    uvrs[uvr['message_id']] = uvr

    # Notify operators of new UVR
    response = {'uvr': uvr, 'notifications': {}}
    operators = [op for op in grid_contents['data']['operators'] if op['announcement_level'] == 'ALL']
    for op in operators:
      url = os.path.join(op['uss_baseurl'], 'uvrs', uvr['message_id'])
      if grid_client.uss_baseurl in url:
        log.debug('Skipping notifying %s of UVR', url)
        continue
      log.debug('Notifying %s of UVR', url)
      result = requests.put(url, headers=grid_client.get_header(interuss_platform.UVR_SCOPE), json=uvr, timeout=5)
      response['notifications'][op['uss']] = {'code': result.status_code, 'content': result.content}
      log.info('Notified %s of UVR: %d %s', url, result.status_code, result.content)

    return flask.jsonify(response)
  elif flask.request.method == 'DELETE':
    log.debug('UVR deletion requested: %s', message_id)
    _validate_control()
    if message_id in uvrs:
      # We're aware of this UVR; that makes removal easy
      try:
        grid_client.remove_uvr(uvrs[message_id])
        del uvrs[message_id]
      except requests.HTTPError as e:
        return _error(e.response.status_code, str(e))
    else:
      log.info('UVR %s not found; reading from grid', message_id)
      # We're not aware of this UVR; let's try to get details from the grid
      uvr = flask.request.json
      area = [interuss_platform.Coord(p[1], p[0]) for p in uvr['geography']['coordinates'][0]]
      _, grid_uvrs, _ = grid_client.get_operators_by_area(area)
      grid_uvrs = [u for u in grid_uvrs if u['message_id'] == message_id]
      if grid_uvrs:
        # Delete the UVR according to the details retrieved from the grid
        uvr = grid_uvrs[0]
        try:
          grid_client.remove_uvr(uvr)
        except requests.HTTPError as e:
          return _error(e.response.status_code, str(e))
      else:
        # The requested UVR doesn't seem to be in the grid
        return _error(status.HTTP_404_NOT_FOUND, 'UVR %s not found' % message_id)
    return '', status.HTTP_204_NO_CONTENT
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)


@webapp.route('/notifications/<notification_key>', methods=['GET'])
def get_notifications(notification_key):
  log.debug('Notifications requested: %s', notification_key)
  _validate_control()
  with notification_lock:
    if notification_key in notification_logs:
      return flask.jsonify(notification_logs[notification_key])
    else:
      return _error(
        status.HTTP_404_NOT_FOUND, 'Key %s not found' % notification_key)


@webapp.route('/notifications', methods=['GET', 'DELETE'])
def del_notifications():
  log.debug('Notifications queried')
  _validate_control()
  if flask.request.method == 'GET':
    if _string_to_bool(flask.request.args.get('details', 'false')):
      with notification_lock:
        uss_filter = flask.request.args.get('uss', None)
        if uss_filter:
          logs = {key: [e for e in msgs if e['uss'] == uss_filter]
                  for key, msgs in notification_logs.items()}
        else:
          logs = notification_logs
        exclude_content = _string_to_bool(
          flask.request.args.get('exclude_content', 'false'))
        if exclude_content:
          logs = {key: [{k: v for k, v in value.items() if k != 'content'}
                        for value in values]
                  for key, values in logs.items()}
        return flask.jsonify(logs)
    else:
      return flask.jsonify({key: [e['source'] + ' ' + e['received']
                                  for e in value]
                            for key, value in notification_logs.items()})
  elif flask.request.method == 'DELETE':
    with notification_lock:
      n = len(notification_logs)
      notification_logs.clear()
      return 'Deleted %d notification keys' % n
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)

# == USS endpoints ==

@webapp.route('/uvrs/<message_id>', methods=['PUT'])
def uvrs_endpoint(message_id):
  log.debug('USS/uvrs accessed')
  _validate_access_token()
  entry = _log_request_body(message_id, 'uvrs')
  log.info('>> Notified of UVR update from %s with ID %s',
           entry['uss'], message_id)
  return '', status.HTTP_204_NO_CONTENT


@webapp.route('/utm_messages/<message_id>', methods=['PUT'])
def utm_messages_endpoint(message_id):
  log.debug('USS/utm_messages accessed')
  _validate_access_token()
  entry = _log_request_body(
    message_id, 'utm_messages', flask.request.json.get('message_type', '???'))
  log.info('>> Notified of UTM message %s from %s with ID %s',
           entry['action'], entry['uss'], message_id)
  return '', status.HTTP_204_NO_CONTENT


@webapp.route('/uss/<uss_instance_id>', methods=['PUT'])
def uss_instances_endpoint(uss_instance_id):
  log.debug('USS/uss accessed')
  _validate_access_token()
  entry = _log_request_body(uss_instance_id, 'uss')
  log.info('>> Notified of USS update from %s with ID %s',
           entry['uss'], uss_instance_id)
  return '', status.HTTP_204_NO_CONTENT


@webapp.route('/negotiations/<message_id>', methods=['PUT'])
def negotiations_endpoint(message_id):
  log.debug('>> !!!USS/negotiations request received with message ID %s',
            message_id)
  _validate_access_token()
  _log_request_body(message_id, 'negotiations')
  return '', status.HTTP_204_NO_CONTENT


@webapp.route('/positions/<position_id>', methods=['PUT'])
def positions_endpoint(position_id):
  log.debug('USS/positions accessed')
  _validate_access_token()
  entry = _log_request_body(position_id, 'positions')
  log.info('>> Notified of position update from %s with ID %s',
           entry['uss'], position_id)
  return '', status.HTTP_204_NO_CONTENT


@webapp.route('/operations', methods=['GET'])
def get_operations_endpoint():
  log.debug('USS/operations queried')
  _validate_access_token()
  return flask.jsonify(operations.values())


@webapp.route('/operations/<gufi>', methods=['GET', 'PUT'])
def operation_endpoint(gufi):
  log.debug('USS/operations/gufi accessed for GUFI %s', gufi)
  _validate_access_token()
  if flask.request.method == 'GET':
    if gufi in operations:
      return flask.jsonify(operations[gufi])
    else:
      flask.abort(status.HTTP_404_NOT_FOUND, 'No operation with GUFI ' + gufi)
  elif flask.request.method == 'PUT':
    entry = _log_request_body(
      gufi, 'operations', flask.request.json.get('state', '???'))
    log.info('>> Notified of operation %s received from %s with GUFI %s',
             entry['action'], entry['uss'], gufi)
    return '', status.HTTP_204_NO_CONTENT
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)


@webapp.route('/enhanced/operations/<gufi>', methods=['GET', 'PUT'])
def enhanced_operation_endpoint(gufi):
  log.debug('USS/enhanced/operations accessed for GUFI %s', gufi)
  _validate_access_token()
  if flask.request.method == 'GET':
    flask.abort(status.HTTP_500_INTERNAL_SERVER_ERROR,
                'Enhanced operations endpoint not yet supported')
  elif flask.request.method == 'PUT':
    log.info('>> !!!Notified of enhanced operation received with GUFI %s', gufi)
    _log_request_body(gufi, 'enhanced_operations')
    return '', status.HTTP_204_NO_CONTENT
  else:
    flask.abort(status.HTTP_405_METHOD_NOT_ALLOWED)


@webapp.before_first_request
def before_first_request():
  if control_authorization is None:
    initialize([])


def _validate_control():
  """Return an error response if no authorization to control this USS."""
  if 'Authorization' not in flask.request.headers:
    msg = 'Authorization header was not included in request'
    log.error(msg)
    flask.abort(status.HTTP_401_UNAUTHORIZED, msg)
  if flask.request.headers['Authorization'] != control_authorization:
    msg = 'Not authorized to access this control endpoint'
    log.error(msg)
    flask.abort(status.HTTP_403_FORBIDDEN, msg)


def _validate_access_token(allowed_scopes=None):
  """Return an error response if the provided access token is invalid."""
  if 'Authorization' in flask.request.headers:
    token = flask.request.headers['Authorization'].replace('Bearer ', '')
  elif 'access_token' in flask.request.headers:
    token = flask.request.headers['access_token']
  else:
    flask.abort(status.HTTP_401_UNAUTHORIZED,
                'Access token was not included in request')

  try:
    claims = jwt.decode(token, public_key, algorithms='RS256')
  except jwt.ExpiredSignatureError:
    msg = 'Access token is invalid: token has expired.'
    log.error(msg)
    flask.abort(status.HTTP_401_UNAUTHORIZED, msg)
  except jwt.DecodeError:
    log.error('Access token is invalid and cannot be decoded.')
    flask.abort(status.HTTP_400_BAD_REQUEST,
                'Access token is invalid: token cannot be decoded.')

  if (allowed_scopes is not None and
      not set(claims['scope']).intersection(set(allowed_scopes))):
    flask.abort(status.HTTP_403_FORBIDDEN, 'Scopes included in access token do '
                                           'not grant access to this resource')


def initialize(argv):
  log.debug('Debug-level log messages are visible')
  options = config.parse_options(argv)

  global control_authorization
  control_authorization = options.control_authorization

  global public_key
  if options.authpublickey.startswith('http'):
    log.info('Downloading auth public key from ' + options.authpublickey)
    response = requests.get(options.authpublickey)
    response.raise_for_status()
    public_key = response.content
  else:
    public_key = options.authpublickey
  public_key = public_key.replace(' PUBLIC ', '_PLACEHOLDER_')
  public_key = public_key.replace(' ', '\n')
  public_key = public_key.replace('_PLACEHOLDER_', ' PUBLIC ')

  global grid_client
  grid_client = interuss_platform.Client(
    options.nodeurl, int(options.zoom), options.authurl, options.username,
    options.password, options.baseurl)

  global always_listen
  always_listen = options.always_listen.split(',')

  global min_listen_time
  min_listen_time = formatting.parse_timestamp(options.min_listen_time)
  global max_listen_time
  max_listen_time = formatting.parse_timestamp(options.max_listen_time)

  return options


def main(argv):
  options = initialize(argv)

  log.info('Starting webserver...')
  webapp.run(host=options.server, port=int(options.port))


# This is what starts everything when run directly as an executable
if __name__ == '__main__':
  main(sys.argv)