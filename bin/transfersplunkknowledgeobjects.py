#!/usr/bin/env python

"""
transfersplunkknowledgeobjects.py

Script to transfer Splunk knowledge objects between instances via REST API queries.
Preserves ownership, sharing, and ACLs, and supports various migration filters.

Run this script via splunk cmd python transfersplunkknowledgeobjects.py.

For help information, use the -h flag:  splunk cmd python transfersplunkknowledgeobjects.py -h

Features: 
    - Transfers knowledge objects between Splunk instances via REST API
    - Preserves object ownership and sharing permissions (ACL) unless the -destOwner parameter is specified
    - Automatically creates missing users and applications on destination
    - Supports token-based and password-based authentication
    - Provides comprehensive filtering options for selective migration
    - Includes retry logic with exponential backoff to handle Splunk Cloud API rate limits and network issues

Limitations:
    - Lookup files must exist on the destination before migrating lookup definitions
    - Some object types may have REST API specific constraints (e.g., collections require 'nobody' ownership)
    - Network connectivity and authentication must be maintained throughout the transfer process

"""

import requests
import xml.etree.ElementTree as ET
import logging
from logging.config import dictConfig
import urllib.parse
import argparse
import json
import copy
from datetime import datetime, timedelta
import re
import time
import random
import string
from requests.exceptions import ConnectionError, Timeout
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging for both console and file output
logging_config = dict(
    version=1,
    formatters={
        'f': {'format': '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'}
    },
    handlers={
        'h': {
            'class': 'logging.StreamHandler',
            'formatter': 'f',
            'level': logging.DEBUG
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': '/tmp/transfer_knowledgeobj.log',
            'formatter': 'f',
            'maxBytes': 10485760,
            'level': logging.DEBUG,
            'backupCount': 5
        }
    },
    root={
        'handlers': ['h', 'file'],
        'level': logging.DEBUG,
    },
)

dictConfig(logging_config)

logger = logging.getLogger()

# Create the argument parser
parser = argparse.ArgumentParser(
    description='Migrate Splunk configuration from 1 Splunk search head to another Splunk search head via the REST API'
)
parser.add_argument('-srcURL',
                    help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example',
                    required=True)
parser.add_argument('-destURL',
                    help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example',
                    required=True)
parser.add_argument('-srcUsername', 
                    help='username to use for REST API of srcURL argument')
parser.add_argument('-srcPassword', 
                    help='password to use for REST API of srcURL argument')
parser.add_argument('-srcApp', 
                    help='application name on srcURL to be migrated from', 
                    required=True)
parser.add_argument('-destApp',
                    help='(optional) application name to be used on destURL to be migrated to defaults to srcApp')
parser.add_argument('-destUsername',
                    help='(optional) username to use for REST API of destURL argument defaults to srcUsername')
parser.add_argument('-destPassword',
                    help='(optional) password to use for REST API of destURL argument defaults to srcPassword')
parser.add_argument('-destOwner',
                    help='(optional) override the username on the destination app when creating the objects')
parser.add_argument('-noPrivate',
                    help='(optional) disable the migration of user level / private objects',
                    action='store_true')
parser.add_argument('-noDisabled',
                    help='(optional) disable the migration of objects with a disabled status in Splunk',
                    action='store_true')
parser.add_argument('-all', 
                    help='(optional) migrate all knowledge objects', action='store_true')
parser.add_argument('-macros', 
                    help='(optional) migrate macro knowledge objects', 
                    action='store_true')
parser.add_argument('-tags',
                     help='(optional) migrate tag knowledge objects', 
                     action='store_true')
parser.add_argument('-eventtypes', 
                    help='(optional) migrate event types knowledge objects', 
                    action='store_true')
parser.add_argument('-allFieldRelated', 
                    help='(optional) migrate all objects under fields', 
                    action='store_true')
parser.add_argument('-calcFields', 
                    help='(optional) migrate calc fields knowledge objects', 
                    action='store_true')
parser.add_argument('-fieldAlias', 
                    help='(optional) migrate field alias knowledge objects', 
                    action='store_true')
parser.add_argument('-fieldExtraction',
                    help='(optional) migrate field extraction knowledge objects',
                    action='store_true')
parser.add_argument('-fieldTransforms', 
                    help='(optional) migrate field transformation knowledge objects '
                         '(excludes nullQueue formatted transforms)',
                    action='store_true',)
parser.add_argument('-lookupDefinition',
                    help='(optional) migrate lookup definition knowledge objects',
                    action='store_true')
parser.add_argument('-workflowActions', 
                    help='(optional) migrate workflow actions', 
                    action='store_true')
parser.add_argument('-sourcetypeRenaming',
                    help='(optional) migrate sourcetype renaming', 
                    action='store_true')
parser.add_argument('-automaticLookup',
                    help='(optional) migrate automation lookup knowledge objects',
                    action='store_true')
parser.add_argument('-datamodels', 
                    help='(optional) migrate data model knowledge objects', 
                    action='store_true')
parser.add_argument('-dashboards',
                    help='(optional) migrate dashboards (user interface -> views)',
                    action='store_true')
parser.add_argument('-savedsearches',
                    help='(optional) migrate saved search objects (this includes reports/alerts)',
                    action='store_true')
parser.add_argument('-navMenu', 
                    help='(optional) migrate navigation menus', 
                    action='store_true')
parser.add_argument('-overrideMode',
                    help='(optional) if the remote knowledge object exists, overwrite it with the migrated '
                         'version only if the migrated version does not have a newer updated time. Skip otherwise',
                    action='store_true')
parser.add_argument('-overrideAlwaysMode',
                    help='(optional) if the remote knowledge object exists, overwrite it with the migrated version '
                         '(by default this will not override)',
                    action='store_true')
parser.add_argument('-collections', 
                    help='(optional) migrate collections (kvstore collections)', 
                    action='store_true')
parser.add_argument('-times',
                     help='(optional) migrate time labels (conf-times)', 
                     action='store_true')
parser.add_argument('-panels',
                     help='(optional) migrate pre-built dashboard panels', 
                     action='store_true')
parser.add_argument('-debugMode',
                    help='(optional) turn on DEBUG level logging (defaults to INFO)',
                    action='store_true')
parser.add_argument('-printPasswords',
                    help='(optional) print passwords in the log files (dev only)',
                    action='store_true')
parser.add_argument('-includeEntities',
                    help='(optional) comma separated list of object values to include (double quoted)')
parser.add_argument('-excludeEntities',
                    help='(optional) comma separated list of object values to exclude (double quoted)')
parser.add_argument('-includeOwner',
                    help='(optional) comma separated list of owners objects that should be transferred (double '
                         'quoted)')
parser.add_argument('-excludeOwner',
                    help='(optional) comma separated list of owners objects that should be transferred (double '
                         'quoted)')
parser.add_argument('-privateOnly', 
                    help='(optional) Only transfer private objects')
parser.add_argument('-viewstates', 
                    help='(optional) migrate viewstates', 
                    action='store_true')
parser.add_argument('-ignoreViewstatesAttribute',
                    help='(optional) when creating saved searches strip the vsid parameter/attribute before '
                         'attempting to create the saved search',
                    action='store_true')
parser.add_argument('-disableAlertsOrReportsOnMigration',
                    help='(optional) when creating alerts/reports, set disabled=1 (or enableSched=0) irrelevant '
                         'of the previous setting pre-migration',
                    action='store_true')
parser.add_argument('-nameFilter',
                    help='(optional) use a regex filter to find the names of the objects to transfer')
parser.add_argument('-sharingFilter',
                    help='(optional) only transfer objects with this level of sharing (user, app, global)',
                    choices=['user', 'app', 'global'])
parser.add_argument('-srcAuthtype',
                    help='(optional) source authentication type',
                    choices=['password', 'token'],
                    default='password')
parser.add_argument('-destAuthtype',
                    help='(optional) destination authentication type',
                    choices=['password', 'token'],
                    default='password')
parser.add_argument('-srcToken',
                    help='(optional) token to use for REST API of srcURL argument (required if srcAuthtype=token)')
parser.add_argument('-destToken',
                    help='(optional) token to use for REST API of destURL argument (required if destAuthtype=token)')
parser.add_argument('-skipDefaults', 
                    help='(optional) the endpoint /services/properties/<type>/default can be used to list default options. Can set this to skip source, destination or none to disable the skipping of these attributes. Only works for savedsearches',
                    choices=['source','destination','none'],
                    default='destination')

args = parser.parse_args()

# If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

# Validate authentication parameters
if args.srcAuthtype == 'token':
    if not args.srcToken:
        parser.error("srcToken is required when srcAuthtype=token")
elif args.srcAuthtype == 'password':
    if not args.srcUsername or not args.srcPassword:
        parser.error("srcUsername and srcPassword are required when srcAuthtype=password")

if args.destAuthtype == 'token':
    if not args.destToken:
        parser.error("destToken is required when destAuthtype=token")
elif args.destAuthtype == 'password':
    if not args.destUsername or not args.destPassword:
        parser.error("destUsername and destPassword are required when destAuthtype=password")

srcApp = args.srcApp
if args.destApp:
    destApp = args.destApp
else:
    destApp = srcApp

if args.srcUsername:
    srcUsername = args.srcUsername
else:
    srcUsername = ""

if args.destUsername:
    destUsername = args.destUsername
else:
    destUsername = ""

if args.srcPassword:
    srcPassword = args.srcPassword
else:
    srcPassword = ""

if args.destPassword:
    destPassword = args.destPassword
else:
    destPassword = ""

if args.srcToken:
    srcToken = args.srcToken
else:
    srcToken = ""

if args.destToken:
    destToken = args.destToken
else:
    destToken = ""

destOwner = False
if args.destOwner:
    destOwner = args.destOwner

if args.nameFilter:
    nameFilter = re.compile(args.nameFilter)

# From server
splunk_rest = args.srcURL

# Destination server
splunk_rest_dest = args.destURL


def make_request(url, method='get', auth_type='password', username='', password='', token='', data=None, verify=False):
    """
    Centralized function to make HTTP requests with either token or password authentication.

    Args:
        url (str): The endpoint URL.
        method (str): HTTP method ('get', 'post', 'delete').
        auth_type (str): Authentication type ('password' or 'token').
        username (str): Username for basic auth.
        password (str): Password for basic auth.
        token (str): Token for bearer auth.
        data (dict): Data to send in POST requests.
        verify (bool): Whether to verify SSL certificates.

    Returns:
        requests.Response: The HTTP response object.
    """
    headers = {}
    auth = None
    max_retries = 5
    base_delay = 1  # seconds

    if auth_type == 'token':
        headers['Authorization'] = f'Bearer {token}'
    else:
        auth = (username, password)

    for attempt in range(max_retries):
        try:
            if method.lower() == 'get':
                return requests.get(url, auth=auth, headers=headers, verify=verify, timeout=30)
            elif method.lower() == 'post':
                return requests.post(url, auth=auth, headers=headers, verify=verify, data=data, timeout=30)
            elif method.lower() == 'delete':
                return requests.delete(url, auth=auth, headers=headers, verify=verify, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except (ConnectionError, Timeout) as e:
            logger.warning(f"Request to {url} failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error(f"Max retries exceeded for {url}.")
                raise  # Re-raise the last exception after all retries fail
        except requests.exceptions.RequestException as e:
            logger.error(f"An unexpected error occurred during request to {url}: {e}")
            raise


def check_and_create_user(host, port, auth_type, username, password, token, user_to_check):
    """
    Check if a user exists on the destination system and create them if they don't

    Args:
        host: Destination Splunk host
        port: Destination Splunk port
        auth_type: Authentication type ('password' or 'token')
        username: Username for auth
        password: Password for auth
        token: Token for auth
        user_to_check: Username to check/create

    Returns:
        bool: True if user exists or was created successfully, False otherwise
    """
    # Extract host from URL if needed
    if host.startswith('https://') or host.startswith('http://'):
        host = host.replace('https://', '').replace('http://', '').split('/')[0]

    # Check if user exists
    check_url = f"https://{host}:{port}/services/authentication/users/{user_to_check}"

    try:
        response = make_request(check_url, method='get', auth_type=auth_type,
                              username=username, password=password, token=token, verify=False)

        if response.status_code == 200:
            logger.debug(f"User '{user_to_check}' already exists on destination")
            return True
        elif response.status_code == 404:
            logger.info(f"User '{user_to_check}' does not exist on destination, creating...")

            # Generate random 16 character password
            random_password = ''.join(random.choices(string.ascii_letters + string.digits + '!@#$%^&*', k=16))

            # Create the user
            create_url = f"https://{host}:{port}/services/authentication/users"
            create_data = {
                'name': user_to_check,
                'password': random_password,
                'roles': 'user',  # Default role
                'force-change-pass': '1'  # Force password change on first login
            }

            create_response = make_request(create_url, method='post', auth_type=auth_type,
                                         username=username, password=password, token=token,
                                         data=create_data, verify=False)

            if create_response.status_code in [200, 201]:
                logger.info(f"Successfully created user '{user_to_check}' on destination")
                logger.info(f"User '{user_to_check}' created with temporary password '{random_password}' " +
                          "and will be forced to change on first login")
                return True
            else:
                logger.error(f"Failed to create user '{user_to_check}': " +
                           f"status={create_response.status_code}, response={create_response.text}")
                return False
        else:
            logger.error(f"Failed to check user '{user_to_check}': " +
                       f"status={response.status_code}, response={response.text}")
            return False

    except Exception as e:
        logger.error(f"Error checking/creating user '{user_to_check}': {e}")
        return False


def get_app_acl(host, port, auth_type, username, password, token, app_name):
    """
    Get ACL information for an app

    Args:
        host: Splunk host
        port: Splunk port
        auth_type: Authentication type ('password' or 'token')
        username: Username for auth
        password: Password for auth
        token: Token for auth
        app_name: App name to get ACL for

    Returns:
        dict: ACL information or None if failed
    """
    # Extract host from URL if needed
    if host.startswith('https://') or host.startswith('http://'):
        host = host.replace('https://', '').replace('http://', '').split('/')[0]

    check_url = f"https://{host}:{port}/services/apps/local/{app_name}?output_mode=json"

    try:
        response = make_request(check_url, method='get', auth_type=auth_type,
                              username=username, password=password, token=token, verify=False)

        if response.status_code == 200:
            data = response.json()
            if 'entry' in data and len(data['entry']) > 0:
                acl = data['entry'][0].get('acl', {})
                logger.debug(f"Retrieved ACL for app '{app_name}': " +
                           f"sharing={acl.get('sharing')}, owner={acl.get('owner')}")
                return acl

        logger.warning(f"Could not retrieve ACL for app '{app_name}': status={response.status_code}")
        return None

    except Exception as e:
        logger.error(f"Error getting ACL for app '{app_name}': {e}")
        return None


def set_app_acl(host, port, auth_type, username, password, token, app_name, acl_info):
    """
    Set ACL on an app to match source ACL

    Args:
        host: Destination Splunk host
        port: Destination Splunk port
        auth_type: Authentication type ('password' or 'token')
        username: Username for auth
        password: Password for auth
        token: Token for auth
        app_name: App name to set ACL on
        acl_info: ACL dictionary from source app

    Returns:
        bool: True if successful, False otherwise
    """
    # Extract host from URL if needed
    if host.startswith('https://') or host.startswith('http://'):
        host = host.replace('https://', '').replace('http://', '').split('/')[0]

    # Get the owner from ACL info
    owner = acl_info.get('owner', 'nobody')

    # Check and create the owner user if needed (unless it's 'nobody')
    if owner != 'nobody':
        user_exists = check_and_create_user(host, port, auth_type, username, password, token, owner)

        if not user_exists:
            logger.warning(f"Cannot create or verify owner '{owner}' for app '{app_name}' ACL - using 'nobody' instead")
            owner = 'nobody'

    acl_url = f"https://{host}:{port}/services/apps/local/{app_name}/acl"

    # Build ACL data from source
    acl_data = {
        'output_mode': 'json',
        'owner': owner,
        'sharing': acl_info.get('sharing', 'app')
    }

    # Add permissions if they exist
    perms = acl_info.get('perms', {})
    if 'read' in perms and perms['read']:
        if isinstance(perms['read'], list):
            acl_data['perms.read'] = ','.join(perms['read'])
        else:
            acl_data['perms.read'] = str(perms['read'])

    if 'write' in perms and perms['write']:
        if isinstance(perms['write'], list):
            acl_data['perms.write'] = ','.join(perms['write'])
        else:
            acl_data['perms.write'] = str(perms['write'])

    try:
        response = make_request(acl_url, method='post', auth_type=auth_type,
                              username=username, password=password, token=token,
                              data=acl_data, verify=False)

        if response.status_code in [200, 201]:
            logger.info(f"Successfully set ACL on app '{app_name}' " +
                       f"(sharing: {acl_data['sharing']}, owner: {acl_data['owner']})")
            return True
        else:
            logger.warning(f"Failed to set ACL on app '{app_name}': " +
                         f"status={response.status_code}, response={response.text}")
            return False

    except Exception as e:
        logger.warning(f"Error setting ACL on app '{app_name}': {e}")
        return False


def check_and_create_app(host, port, auth_type, username, password, token, app_name, source_acl=None):
    """
    Check if an app exists on the destination system and create it if it doesn't

    Args:
        host: Destination Splunk host
        port: Destination Splunk port
        auth_type: Authentication type ('password' or 'token')
        username: Username for auth
        password: Password for auth
        token: Token for auth
        app_name: App name to check/create
        source_acl: ACL info from source app to replicate (optional)

    Returns:
        bool: True if app exists or was created successfully, False otherwise
    """
    # Extract host from URL if needed
    if host.startswith('https://') or host.startswith('http://'):
        host = host.replace('https://', '').replace('http://', '').split('/')[0]

    # Check if app exists
    check_url = f"https://{host}:{port}/services/apps/local/{app_name}"

    try:
        response = make_request(check_url, method='get', auth_type=auth_type,
                              username=username, password=password, token=token, verify=False)

        if response.status_code == 200:
            logger.debug(f"App '{app_name}' already exists on destination")
            return True
        elif response.status_code == 404:
            logger.info(f"App '{app_name}' does not exist on destination, creating...")

            # Create the app
            create_url = f"https://{host}:{port}/services/apps/local"
            create_data = {
                'name': app_name,
                'label': app_name,
                'visible': '1',
                'author': 'auto-created',
                'description': f'Auto-created app during knowledge object transfer'
            }

            create_response = make_request(create_url, method='post', auth_type=auth_type,
                                         username=username, password=password, token=token,
                                         data=create_data, verify=False)

            if create_response.status_code in [200, 201]:
                logger.info(f"Successfully created app '{app_name}' on destination")

                # Set ACL to match source if provided
                if source_acl:
                    set_app_acl(host, port, auth_type, username, password, token, app_name, source_acl)

                return True
            else:
                logger.error(f"Failed to create app '{app_name}': " +
                           f"status={create_response.status_code}, response={create_response.text}")
                return False
        else:
            logger.error(f"Failed to check app '{app_name}': " +
                       f"status={response.status_code}, response={response.text}")
            return False

    except Exception as e:
        logger.error(f"Error checking/creating app '{app_name}': {e}")
        return False


# As per
# https://stackoverflow.com/questions/1101508/how-to-parse-dates-with-0400-timezone-string-in-python/23122493#23122493
def determineTime(timestampStr, name, app, obj_type):
    logger.debug(
        f"Attempting to convert {timestampStr} to timestamp for {name} name, "
        f"in app {app} for type {obj_type}"
    )
    ret = datetime.strptime(timestampStr[0:19], "%Y-%m-%dT%H:%M:%S")
    if timestampStr[19] == '+':
        ret -= timedelta(hours=int(timestampStr[20:22]), minutes=int(timestampStr[24:]))
    elif timestampStr[19] == '-':
        ret += timedelta(hours=int(timestampStr[20:22]), minutes=int(timestampStr[24:]))
    logger.debug(
        f"Converted time is {ret} to timestamp for {name} name, in app {app} "
        f"for type {obj_type}"
    )
    return ret


def appendToResults(resultsDict, name, result):
    if name not in resultsDict:
        resultsDict[name] = []
    resultsDict[name].append(result)


def popLastResult(resultsDict, name):
    if name in resultsDict:
        if len(resultsDict[name]) > 0:
            resultsDict[name].pop()

###########################
#
# runQueries (generic version)
#   This attempts to call the REST API of the srcServer, parses the resulting XML data and re-posts the relevant
#   sections to the destination server
#   This method works for everything excluding macros which have a different process
#   Due to variations in the REST API there are a few hacks inside this method to handle specific use cases,
#   however the majority are straightforward
#
###########################
def runQueries(app, endpoint, obj_type, fieldIgnoreList, destApp, aliasAttributes={}, valueAliases={}, nameOverride="",
               destOwner=False, noPrivate=False, noDisabled=False, override=None, includeEntities=None,
               excludeEntities=None, includeOwner=None, excludeOwner=None, privateOnly=None,
               disableAlertsOrReportsOnMigration=False, overrideAlways=None, actionResults=None):
    # Keep a success/failure list to be returned by this function
    # actionResults = {}

    # Use count=-1 to ensure we see all the objects
    url = f"{splunk_rest}/servicesNS/-/{app}{endpoint}?count=-1"
    logger.debug(f"Running requests.get() on {url} with username {srcUsername} in app {app}")

    # Verify=false is hardcoded to workaround local SSL issues
    res = make_request(
        url, method='get', auth_type=args.srcAuthtype, username=srcUsername,
        password=srcPassword, token=args.srcToken, verify=False
    )
    if res.status_code != requests.codes.ok:
        logger.error(
            f"URL {url} in app {app} status code {res.status_code} reason "
            f"{res.reason}, response: '{res.text}'"
        )

    #Splunk returns data in XML format, use the element tree to work through it
    root = ET.fromstring(res.text)

    infoList = {}
    for child in root:
        # Working per entry in the results
        if child.tag.endswith("entry"):
            # Down to each entry level
            info = {}
            # Some lines of data we do not want to keep, assume we want it
            keep = True
            acl_info = {}  # Store complete ACL information
            for innerChild in child:
                # title / name attribute
                if innerChild.tag.endswith("title"):
                    title = innerChild.text
                    info["name"] = title
                    logger.debug(
                        f"{title} is the name/title of this entry for type {obj_type} in app {app}"
                    )
                    # If we have an include/exclude list we deal with that scenario now
                    if includeEntities:
                        if title not in includeEntities:
                            logger.debug(
                                f"{title} of type {obj_type} not in includeEntities list in app {app}"
                            )
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug(
                                f"{title} of type {obj_type} in excludeEntities list in app {app}"
                            )
                            keep = False
                            break

                    if args.nameFilter:
                        if not nameFilter.search(title):
                            logger.debug(
                                f"{title} of type {obj_type} does not match regex in app {app}"
                            )
                            keep = False
                            break

                    # Backup the original name if we override it, override works fine for creation
                    # but updates require the original name
                    if 'name' in list(aliasAttributes.values()):
                        info["origName"] = title

                elif innerChild.tag.endswith("updated"):
                    updatedStr = innerChild.text
                    updated = determineTime(updatedStr, info["name"], app, obj_type)
                    info['updated'] = updated
                    logger.debug(f"name {title}, type {obj_type} in app {app} was updated on {updated}")
                #Content appears to be where 90% of the data we want is located
                elif innerChild.tag.endswith("content"):
                    for theAttribute in innerChild[0]:
                        # acl has the owner, sharing and app level which we required (sometimes there is eai:app but
                        # it's not 100% consistent so this is safer also have switched from author to owner as it's
                        # likely the safer option...
                        if theAttribute.attrib['name'] == 'eai:acl':
                            for theList in theAttribute[0]:
                                if theList.attrib['name'] == 'sharing':
                                    logger.debug(
                                        f"{info['name']} of type {obj_type} has sharing {theList.text} in app {app}"
                                    )
                                    info["sharing"] = theList.text
                                    acl_info["sharing"] = theList.text
                                    if noPrivate and info["sharing"] == "user":
                                        logger.debug(
                                            f"{info['name']} of type {obj_type} found but the noPrivate flag is true, "
                                            f"excluding this in app {app}"
                                        )
                                        keep = False
                                        break
                                    elif privateOnly and info["sharing"] != "user":
                                        logger.debug(
                                            f"{info['name']} of type {obj_type} found but the privateOnly flag is true "
                                            f"and value of {info['sharing']} is not user level sharing (private), "
                                            f"excluding this in app {app}"
                                        )
                                        keep = False
                                        break

                                    if args.sharingFilter and not args.sharingFilter == info["sharing"]:
                                        logger.debug(
                                            f"{info['name']} of type {obj_type} found but the sharing level is set to "
                                            f"{args.sharingFilter} and this object has sharing {info['sharing']} "
                                            f"excluding this in app {app}"
                                        )
                                        keep = False
                                        break

                                elif theList.attrib['name'] == 'app':
                                    foundApp = theList.text
                                    acl_info["app"] = foundApp
                                    logger.debug(
                                        f"{info['name']} of type {obj_type} in app context of {app} belongs to {foundApp}"
                                    )
                                    #We can see globally shared objects in our app context, it does not mean we should
                                    # migrate them as it's not ours...
                                    if app != foundApp:
                                        logger.debug(
                                            f"{info['name']} of type {obj_type} found in app context of {app} "
                                            f"belongs to app context {foundApp}, excluding from app {app}"
                                        )
                                        keep = False
                                        break
                                #owner is seen as a nicer alternative to the author variable
                                elif theList.attrib['name'] == 'owner':
                                    owner = theList.text
                                    acl_info["owner"] = owner

                                    #If we have include or exlcude owner lists we deal with this now
                                    if includeOwner:
                                        if not owner in includeOwner:
                                            logger.debug(
                                                f"{info['name']} of type {obj_type} with owner {owner} not in "
                                                f"includeOwner list in app {app}"
                                            )
                                            keep = False
                                            break
                                    if excludeOwner:
                                        if owner in excludeOwner:
                                            logger.debug(
                                                f"{info['name']} of type {obj_type} with owner {owner} in "
                                                f"excludeOwner list in app {app}"
                                            )
                                            keep = False
                                            break
                                    logger.debug(
                                        f"{info['name']} of type {obj_type} has owner {owner} in app {app}"
                                    )
                                    info["owner"] = owner
                                # Capture read permissions
                                elif theList.attrib['name'] == 'perms':
                                    if 'perms' not in acl_info:
                                        acl_info['perms'] = {}
                                    for perm in theList:
                                        if not 'name' in perm.attrib:
                                            # empty?
                                            continue
                                        elif perm.attrib['name'] == 'read':
                                            read_perms = []
                                            for item in perm:
                                                read_perms.append(item.text)
                                            acl_info['perms']['read'] = read_perms
                                            logger.debug(f"{info['name']} of type {obj_type} has read permissions "
                                                         f"{read_perms} in app {app}")
                                        elif perm.attrib['name'] == 'write':
                                            write_perms = []
                                            for item in perm:
                                                write_perms.append(item.text)
                                            acl_info['perms']['write'] = write_perms
                                            logger.debug(f"{info['name']} of type {obj_type} has write permissions "
                                                         f"{write_perms} in app {app}")

                        else:
                            #We have other attributes under content, we want the majority of them
                            attribName = theAttribute.attrib['name']

                            # Under some circumstances we want the attribute and contents but we want to call
                            # it a different name...
                            if attribName in aliasAttributes:
                                attribName = aliasAttributes[attribName]

                            #If it's disabled *and* we don't want disabled objects we can determine this here
                            if attribName == "disabled" and noDisabled and theAttribute.text == "1":
                                logger.debug(f"{info['name']} of type {obj_type} is disabled and the noDisabled flag is "
                                             f"true, excluding this in app {app}")
                                keep = False
                                break

                            #Field extractions change from "Uses transform" to "REPORT" And "Inline" to "EXTRACTION"
                            # for some strange reason...therefore we have a list of attribute values that we deal with
                            # here which get renamed to the provided values
                            if theAttribute.text in valueAliases:
                                theAttribute.text = valueAliases[theAttribute.text]

                            logger.debug(f"{info['name']} of type {obj_type} found key/value of "
                                         f"{attribName}={theAttribute.text} in app context {app}" )

                            #Yet another hack, this time to deal with collections using accelrated_fields.<value> when
                            # looking at it via REST GET requests, but requiring accelerated_fields to be used when
                            # POST'ing the value to create the collection!
                            if obj_type == "collections (kvstore definition)" and attribName.find("accelrated_fields") == 0:
                                attribName = "accelerated_fields" + attribName[17:]

                            #Hack to deal with datamodel tables not working as expected
                            if attribName == "description" and obj_type=="datamodels" and \
                                "dataset.type" in info and info["dataset.type"] == "table":
                                #For an unknown reason the table datatype has extra fields in the description
                                # which must be removed however we have to find them first...
                                res = json.loads(theAttribute.text)
                                fields = res['objects'][0]['fields']

                                #We're looking through the dictionary and deleting from it so copy
                                #the dictionary so we can safely iterate through while deleting from the
                                #real copy
                                fieldCopy = copy.deepcopy(fields)

                                for field in fieldCopy:
                                    name = field['fieldName']
                                    logger.debug("name is " + name)
                                    if name != "RootObject":
                                        index = fields.index(field)
                                        del fields[index]

                                res = json.dumps(res)
                                info[attribName] = res
                            #We keep the attributes that are not None
                            elif theAttribute.text:
                                info[attribName] = theAttribute.text
                            #A hack related to automatic lookups, where a None / empty value must be sent through
                            # as "", otherwise requests will strip the entry from the post request. In the case of
                            # an automatic lookup we want to send through the empty value...
                            elif obj_type=="automatic lookup" and theAttribute.text == None:
                                info[attribName] = ""
            #If we have not set the keep flag to False
            if keep:
                # Store the complete ACL information
                info["acl_info"] = acl_info

                # Exclude default properties as they do not need to be set (as this just causes configuration file bloat in savedsearches)
                # This endpoint appears to only work for savedsearches
                if args.skipDefaults == "destination" and obj_type == "savedsearches":
                    url = f"{splunk_rest_dest}/services/properties/{obj_type}/default?output_mode=json"
                    res = make_request(url, method='get', auth_type=args.destAuthtype, username=destUsername,
                    password=destPassword, token=args.destToken, verify=False).json()
                elif args.skipDefaults == "source" and obj_type == "savedsearches":
                    url = f"{splunk_rest}/services/properties/{obj_type}/default?output_mode=json"
                    res = make_request(url, method='get', auth_type=args.srcAuthtype, username=srcUsername,
                    password=srcPassword, token=args.srcToken, verify=False).json()

                default_values = {}

                if args.skipDefaults == "none" or obj_type != "savedsearches" or 'messages' in res and res['messages'][0]['text'].find(f"{obj_type} does not exist") != -1:
                    pass
                else:
                    for entry in res['entry']:
                        default_values[entry['name']] = entry['content']

                #Some attributes are not used to create a new version so we remove them...(they may have been used above first so we kept them until now)
                for attribName in default_values:
                    if attribName in info and (info[attribName] == default_values[attribName]):
                        logger.debug(f"Removing property {info[attribName]} from {info['name']} of type {obj_type} in app context "
                            f"{app} with owner {info['owner']}")
                        del info[attribName]

                if nameOverride != "":
                    info["origName"] = info["name"]
                    info["name"] = info[nameOverride]
                    # TODO: Hack to handle field extractions where they have an extra piece of
                    # info in the name, as in the name is prepended with EXTRACT-, REPORT- or
                    # LOOKUP-. We need to remove this before creating the new version.
                    if obj_type == "fieldextractions" and info["name"].find("EXTRACT-") == 0:
                        logger.debug(
                            f"Overriding name of {info['name']} of type {obj_type} in app context "
                            f"{app} with owner {info['owner']} to new name of {info['name'][8:]}"
                        )
                        info["name"] = info["name"][8:]
                    elif obj_type == "fieldextractions" and info["name"].find("REPORT-") == 0:
                        logger.debug(
                            f"Overriding name of {info['name']} of type {obj_type} in app context "
                            f"{app} with owner {info['owner']} to new name of {info['name'][7:]}"
                        )
                        info["name"] = info["name"][7:]
                    elif obj_type == "automatic lookup" and info["name"].find("LOOKUP-") == 0:
                        logger.debug(
                            f"Overriding name of {info['name']} of type {obj_type} in app context "
                            f"{app} with owner {info['owner']} to new name of {info['name'][7:]}"
                        )
                        info["name"] = info["name"][7:]
                    elif obj_type == "fieldaliases":
                        newName = info["name"]
                        newName = newName[newName.find("FIELDALIAS-") + 11:]
                        logger.debug(
                            f"Overriding name of {info['name']} of type {obj_type} in app context "
                            f"{app} with owner {info['owner']} to new name of {newName}"
                        )
                        info["name"] = newName
                #Some attributes are not used to create a new version so we remove them...(they may have been used above first so we kept them until now)
                for attribName in fieldIgnoreList:
                    if attribName in info:
                        del info[attribName]

                #Add this to the infoList
                sharing = info["sharing"]
                if sharing not in infoList:
                    infoList[sharing] = []

                # REST API does not support the creation of null queue entries as tested in 7.0.5 and 7.2.1,
                # these are also unused on search heads anyway so ignoring these with a warning
                if obj_type == "fieldtransformations" and "FORMAT" in info and info["FORMAT"] == "nullQueue":
                    logger.warning(
                        f"Dropping the transfer of {info['name']} of type {obj_type} in app context "
                        f"{app} with owner {info['owner']} because nullQueue entries cannot be "
                        f"created via REST API (and they are not required in search heads)"
                    )
                else:
                    infoList[sharing].append(info)
                    logger.info(
                        f"Recording {obj_type} info for {info['name']} in app context {app} "
                        f"with owner {info['owner']}"
                    )

                # If we are migrating but leaving the old app enabled in a previous environment
                # we may not want to leave the report and/or alert enabled
                if disableAlertsOrReportsOnMigration and obj_type == "savedsearches":
                    if "disabled" in info and info["disabled"] == "0" and "alert_condition" in info:
                        logger.info(
                            f"{obj_type} of type {info['name']} (alert) in app {app} with owner "
                            f"{info['owner']} was enabled but disableAlertsOrReportOnMigration set, "
                            f"setting to disabled"
                        )
                        info["disabled"] = 1
                    elif "is_scheduled" in info and "alert_condition" not in info and \
                            info["is_scheduled"] == "1":
                        info["is_scheduled"] = 0
                        logger.info(
                            f"{obj_type} of type {info['name']} (scheduled report) in app {app} with "
                            f"owner {info['owner']} was enabled but disableAlertsOrReportOnMigration "
                            f"set, setting to disabled"
                        )
    app = destApp

    # Cycle through each one we need to migrate. We process global/app/user
    # as users can duplicate app level objects with the same names, but we create
    # everything at user level first then re-own it, so global/app must happen first.
    if "global" in infoList:
        logger.debug(
            f"Now running runQueriesPerList with knowledge objects of type {obj_type} "
            f"with global level sharing in app {app}"
        )
        runQueriesPerList(
            infoList["global"], destOwner, obj_type, override, app, splunk_rest_dest,
            endpoint, actionResults, overrideAlways
        )

    if "app" in infoList:
        logger.debug(
            f"Now running runQueriesPerList with knowledge objects of type {obj_type} "
            f"with app level sharing in app {app}"
        )
        runQueriesPerList(
            infoList["app"], destOwner, obj_type, override, app, splunk_rest_dest,
            endpoint, actionResults, overrideAlways
        )

    if "user" in infoList:
        logger.debug(
            f"Now running runQueriesPerList with knowledge objects of type {obj_type} "
            f"with user (private) level sharing in app {app}"
        )
        runQueriesPerList(
            infoList["user"], destOwner, obj_type, override, app, splunk_rest_dest,
            endpoint, actionResults, overrideAlways
        )

    return actionResults

###########################
#
# runQueriesPerList
#   Runs the required queries to create the knowledge object and then re-owns them to the correct user
#
###########################
def runQueriesPerList(infoList, destOwner, obj_type, override, app, splunk_rest_dest, endpoint, 
                      actionResults, overrideAlways):
    # Cache for users we've already checked/created
    checked_users = set()

    for anInfo in infoList:
        sharing = anInfo["sharing"]
        owner = anInfo["owner"]
        acl_info = anInfo.get("acl_info", {})  # Get stored ACL info

        if destOwner:
            owner = destOwner

        # Check if owner exists on destination system and create if needed
        if owner not in checked_users and owner != 'nobody':
            # Extract host and port from splunk_rest_dest URL
            dest_parts = splunk_rest_dest.replace('https://', '').replace('http://', '').split(':')
            dest_host = dest_parts[0]
            dest_port = dest_parts[1] if len(dest_parts) > 1 else '8089'

            user_exists = check_and_create_user(
                dest_host, dest_port, args.destAuthtype,
                destUsername, destPassword, destToken, owner
            )

            if not user_exists:
                logger.error(f"Cannot create or verify user '{owner}' on destination system")
                appendToResults(actionResults, 'creationFailure', f"User creation failed for {anInfo['name']}")
                continue

            checked_users.add(owner)

        #We cannot post the sharing/owner information to the REST API, we use them later
        del anInfo["sharing"]
        del anInfo["owner"]
        if "acl_info" in anInfo:
            del anInfo["acl_info"]  # Remove ACL info from payload

        payload = anInfo
        name = anInfo["name"]
        curUpdated = anInfo["updated"]
        del anInfo["updated"]

        url = f"{splunk_rest_dest}/servicesNS/{owner}/{app}/{endpoint}"
        objURL = None
        origName = None
        encoded_name = urllib.parse.quote(name.encode('utf-8'))
        encoded_name = encoded_name.replace("/", "%2F")

        if 'origName' in anInfo:
            origName = anInfo['origName']
            del anInfo['origName']
            logger.debug(
                f"{name} of type {obj_type} overriding name from {name} to {origName} "
                f"due to origName existing in config dictionary"
            )
            objURL = f"{splunk_rest_dest}/servicesNS/-/{app}/{endpoint}/{origName}?output_mode=json"
        else:
            # datamodels do not allow /-/ (or private / user level sharing, only app level)
            if obj_type == "datamodels":
                objURL = (
                    f"{splunk_rest_dest}/servicesNS/{owner}/{app}/{endpoint}/"
                    f"{encoded_name}?output_mode=json"
                )
            else:
                objURL = (
                    f"{splunk_rest_dest}/servicesNS/-/{app}/{endpoint}/"
                    f"{encoded_name}?output_mode=json"
                )
        logger.debug(f"{name} of type {obj_type} checking on URL {objURL} to see if it exists")

        # Verify=false is hardcoded to workaround local SSL issues
        res = make_request(
            objURL, method='get', auth_type=args.destAuthtype, username=destUsername,
            password=destPassword, token=args.destToken, verify=False
        )
        objExists = False
        updated = None
        createdInAppContext = False

        # If we get 404 it definitely does not exist
        if (res.status_code == 404):
            logger.debug(f"URL {objURL} is throwing a 404, assuming new object creation")
        elif (res.status_code != requests.codes.ok):
            logger.error(
                f"URL {objURL} in app {app} status code {res.status_code} "
                f"reason {res.reason}, response: '{res.text}'"
            )
        else:
            # However, the fact that we did not get a 404 does not mean it exists
            # in the context we expect it to. Perhaps it's global and from another app context?
            # Or perhaps it's app level but we're restoring a private object...
            logger.debug(f"Attempting to JSON loads on {res.text}")
            resDict = json.loads(res.text)
            for entry in resDict['entry']:
                sharingLevel = entry['acl']['sharing']
                appContext = entry['acl']['app']
                updatedStr = entry['updated']
                remoteObjOwner = entry['acl']['owner']
                updated = determineTime(updatedStr, name, app, obj_type)

                if (appContext == app and (sharing == 'app' or sharing == 'global') and
                        (sharingLevel == 'app' or sharingLevel == 'global')):
                    objExists = True
                    logger.debug(
                        f"name {name} of type {obj_type} in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                elif (appContext == app and sharing == 'user' and
                    sharingLevel == "user" and remoteObjOwner == owner):
                    objExists = True
                    logger.debug(
                        f"name {name} of type {obj_type} in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                elif (appContext == app and sharingLevel == "user" and
                    remoteObjOwner == owner and (sharing == "app" or sharing == "global")):
                    logger.debug(
                        f"name {name} of type {obj_type} in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                else:
                    logger.debug(
                        f"name {name} of type {obj_type} in app context {app}, found the "
                        f"object with this name in sharingLevel {sharingLevel} and "
                        f"appContext {appContext}, updated time of {updated}, url {objURL}"
                    )        

        #Hack to handle the times (conf-times) not including required attributes for creation in existing entries
        #not sure how this happens but it fails to create in 7.0.5 but works fine in 7.2.x, fixing for the older versions
        if obj_type == "times (conf-times)" and "is_sub_menu" not in payload:
            payload["is_sub_menu"] = "0"

        if sharing == 'app' or sharing == 'global':
            url = f"{splunk_rest_dest}/servicesNS/nobody/{app}/{endpoint}"
            logger.info(
                f"name {name} of type {obj_type} in app context {app}, sharing level is "
                f"non-user so creating with nobody context updated url is {url}"
            )
            createdInAppContext = True

        deletionURL = None
        if objExists is False:
            logger.debug(
                f"Attempting to create {obj_type} with name {name} on URL {url} with "
                f"payload '{payload}' in app {app}"
            )
            res = make_request(
                url, method='post', auth_type=args.destAuthtype, username=destUsername,
                password=destPassword, token=args.destToken, data=payload, verify=False
            )
            if (res.status_code != requests.codes.ok and res.status_code != 201):
                logger.error(
                    f"{name} of type {obj_type} with URL {url} status code {res.status_code} "
                    f"reason {res.reason}, response '{res.text}', in app {app}, owner {owner}"
                )
                appendToResults(actionResults, 'creationFailure', name)
                continue
            else:
                logger.debug(
                    f"{name} of type {obj_type} in app {app} with URL {url} result is: "
                    f"'{res.text}' owner of {owner}"
                )

            # Parse the result to find the new URL to use
            root = ET.fromstring(res.text)
            infoList = [] # This variable assignment seems to clear infoList, ensure this is intended.

            creationSuccessRes = False
            for child in root:
                # Working per entry in the results
                if child.tag.endswith("entry"):
                    # Down to each entry level
                    for innerChild in child:
                        # print innerChild.tag
                        if innerChild.tag.endswith("link") and innerChild.attrib["rel"] == "list":
                            deletionURL = f"{splunk_rest_dest}/{innerChild.attrib['href']}"
                            logger.debug(
                                f"{name} of type {obj_type} in app {app} recording deletion "
                                f"URL as {deletionURL}"
                            )
                            appendToResults(actionResults, 'creationSuccess', deletionURL)
                            creationSuccessRes = True
                elif child.tag.endswith("messages"):
                    for innerChild in child:
                        if (innerChild.tag.endswith("msg") and
                                (innerChild.attrib["type"] == "ERROR" or "WARN" in innerChild.attrib)):
                            logger.warning(
                                f"{name} of type {obj_type} in app {app} had a warn/error "
                                f"message of '{innerChild.text}' owner of {owner}"
                            )
                            # Sometimes the object appears to be created but is unusable,
                            # which is annoying. At least provide the warning to the logs
                            # and record it in the failure list for investigation.
                            appendToResults(actionResults, 'creationFailure', name)
            if not deletionURL:
                logger.warning(
                    f"{name} of type {obj_type} in app {app} did not appear to create "
                    f"correctly, will not attempt to change the ACL of this item"
                )
                continue
            # Re-owning it to the previous owner
            url = f"{deletionURL}/acl"
            payload = {"owner": owner, "sharing": sharing}
            logger.info(
                f"Attempting to change ownership of {obj_type} with name {name} via URL {url} "
                f"to owner {owner} in app {app} with sharing {sharing}"
            )
            res = make_request(
                url, method='post', auth_type=args.destAuthtype, username=destUsername,
                password=destPassword, token=args.destToken, data=payload, verify=False
            )

            # If re-own fails consider this a failure that requires investigation
            if (res.status_code != requests.codes.ok):
                logger.error(
                    f"{name} of type {obj_type} in app {app} with URL {url} status code "
                    f"{res.status_code} reason {res.reason}, response '{res.text}', "
                    f"owner of {owner}"
                )
                appendToResults(actionResults, 'creationFailure', name)
                if res.status_code == 409:
                    if obj_type == "eventtypes":
                        logger.warning(
                            f"Received a 409 while changing the ACL permissions of {name} "
                            f"of type {obj_type} in app {app} with URL {url}, however "
                            f"eventtypes throw this error and work anyway. Ignoring!"
                        )
                        continue
                    # Delete the duplicate private object rather than leave it there for no reason
                    url = url[:-4]
                    logger.warning(
                        f"Deleting the private object as it could not be re-owned {name} "
                        f"of type {obj_type} in app {app} with URL {url}"
                    )
                    make_request(
                        url, method='delete', auth_type=args.destAuthtype,
                        username=destUsername, password=destPassword,
                        token=args.destToken, verify=False
                    )
                    # If we previously recorded success, remove that entry
                    if creationSuccessRes:
                        popLastResult(actionResults, 'creationSuccess')
                continue
            else:
                logger.debug(
                    f"{name} of type {obj_type} in app {app}, ownership changed. Response: "
                    f"{res.text}. Will update deletion URL. Owner: {owner}, Sharing: {sharing}"
                )

                #Parse the return response
                root = ET.fromstring(res.text)
                infoList = []
                for child in root:
                    #Working per entry in the results
                    if child.tag.endswith("entry"):
                        #Down to each entry level
                        for innerChild in child:
                            if innerChild.tag.endswith("link") and innerChild.attrib["rel"]=="list":
                                deletionURL = f"{splunk_rest_dest}/{innerChild.attrib['href']}"
                                logger.debug(
                                    f"{name} of type {obj_type} in app {app} recording new deletion URL: {deletionURL}. "
                                    f"Owner: {owner}, Sharing: {sharing}"
                                )
                                #Remove our last recorded URL
                                popLastResult(actionResults, 'creationSuccess')
                                appendToResults(actionResults, 'creationSuccess', deletionURL)
            if creationSuccessRes:
                logger.info(
                    f"Created {name} of type {obj_type} in app {app}. Owner: {owner}, Sharing: {sharing}"
                )
            else:
                logger.warning(
                    f"Attempted to create {name} of type {obj_type} in app {app} (Owner: {owner}, Sharing: {sharing}) "
                    f"but failed"
                )
        else:
            # object exists already
            if override or overrideAlways:
                # If the override flag is on and the curUpdated attribute is older than the remote updated attribute,
                # then do not continue with the override. If we have overrideAlways we blindly overwrite.
                if override and curUpdated <= updated:
                    logger.info(
                        f"{name} of type {obj_type} in app {app} with URL {objURL}, owner {owner}, "
                        f"source object says time of {curUpdated}, destination object says time of "
                        f"{updated}, skipping this entry"
                    )
                    appendToResults(actionResults, 'creationSkip', objURL)
                    continue
                else:
                    logger.info(
                        f"{name} of type {obj_type} in app {app} with URL {objURL}, owner {owner}, "
                        f"source object says time of {curUpdated}, destination object says time of "
                        f"{updated}, will update this entry"
                    )
                url = objURL

                # If we're user level we want to update using the user endpoint
                # If it's app/global we update using the nobody to ensure we're updating the app level object
                urlName = origName if origName else encoded_name

                if sharing == "user":
                    url = f"{splunk_rest_dest}/servicesNS/{owner}/{app}/{endpoint}/{urlName}"
                else:
                    url = f"{splunk_rest_dest}/servicesNS/nobody/{app}/{endpoint}/{urlName}"

                # Cannot post type/stanza when updating field extractions or a few other object types,
                # but require them for creation?!
                if 'type' in payload:
                    del payload['type']
                if 'stanza' in payload:
                    del payload['stanza']

                # Remove the name from the payload
                del payload['name']
                logger.debug(
                    f"Attempting to update {obj_type} with name {name} on URL {url} with "
                    f"payload '{payload}' in app {app}"
                )
                res = make_request(
                    url, method='post', auth_type=args.destAuthtype, username=destUsername,
                    password=destPassword, token=args.destToken, data=payload, verify=False
                )
                if res.status_code != requests.codes.ok and res.status_code != 201:
                    logger.error(
                        f"{name} of type {obj_type} with URL {url} status code {res.status_code} "
                        f"reason {res.reason}, response '{res.text}', in app {app}, owner {owner}"
                    )
                    appendToResults(actionResults, 'updateFailure', name)
                else:
                    logger.debug(
                        f"Post-update of {name} of type {obj_type} in app {app} with URL {url} "
                        f"result is: '{res.text}' owner of {owner}"
                    )
                    appendToResults(actionResults, 'updateSuccess', name)

                # Re-owning it to the previous owner
                if sharing != "user":
                    url = f"{url}/acl"
                    payload = {"owner": owner, "sharing": sharing}
                    logger.info(
                        f"App or Global sharing in use, attempting to change ownership of "
                        f"{obj_type} with name {name} via URL {url} to owner {owner} in app "
                        f"{app} with sharing {sharing}"
                    )
                    res = make_request(
                        url, method='post', auth_type=args.destAuthtype, username=destUsername,
                        password=destPassword, token=args.destToken, data=payload, verify=False
                    )
            else:
                appendToResults(actionResults, 'creationSkip', objURL)
                logger.info(
                    f"{name} of type {obj_type} in app {app} owner of {owner}, object already "
                    f"exists and override is not set, nothing to do here"
                )

###########################
#
# macros
#
###########################
# macro use cases are slightly different to everything else on the REST API
# enough that this code has not been integrated into the runQuery() function
def macros(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner,
           excludeOwner, privateOnly, override, overrideAlways, macroResults):
    macros = {}
    # servicesNS/-/-/properties/macros doesn't show private macros so using /configs/conf-macros to find all the macros
    # again with count=-1 to find all the available macros
    url = f"{splunk_rest}/servicesNS/-/{app}/configs/conf-macros?count=-1"
    logger.debug(
        f"Running requests.get() on {url} with username {srcUsername} in app {app} for type macro"
    )
    res = make_request(
        url, method='get', auth_type=args.srcAuthtype, username=srcUsername,
        password=srcPassword, token=args.srcToken, verify=False
    )
    if res.status_code != requests.codes.ok:
        logger.error(
            f"Type macro in app {app}, URL {url} status code {res.status_code} reason {res.reason}, "
            f"response '{res.text}'"
        )

    # Parse the XML tree
    root = ET.fromstring(res.text)

    for child in root:
        # Working per entry in the results
        if child.tag.endswith("entry"):
            # Down to each entry level
            macroInfo = {}
            keep = True
            for innerChild in child:
                # title is the name
                if innerChild.tag.endswith("title"):
                    title = innerChild.text
                    macroInfo["name"] = title
                    logger.debug(f"Found macro title/name: {title} in app {app}")
                    # Deal with the include/exclude lists
                    if includeEntities:
                        if title not in includeEntities:
                            logger.debug(
                                f"{title} of type macro not in includeEntities list in app {app}"
                            )
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug(
                                f"{title} of type macro in excludeEntities list in app {app}"
                            )
                            keep = False
                            break

                    if args.nameFilter:
                        if not nameFilter.search(title):
                            logger.debug(
                                f"{title} of type macro does not match regex in app {app}"
                            )
                            keep = False
                            break

                elif innerChild.tag.endswith("updated"):
                    updatedStr = innerChild.text
                    updated = determineTime(updatedStr, macroInfo["name"], app, "macro")
                    macroInfo['updated'] = updated
                    logger.debug(
                        f"name {title}, of type macro in app {app} was updated on {updated}"
                    )
                # Content appears to be where 90% of the data we want is located
                elif innerChild.tag.endswith("content"):
                    for theAttribute in innerChild[0]:
                        # acl has the owner, sharing and app level which we required
                        # (sometimes there is eai:app but it's not 100% consistent so this is
                        # safer also have switched from author to owner as it's likely the
                        # safer option...)
                        if theAttribute.attrib['name'] == 'eai:acl':
                            for theList in theAttribute[0]:
                                if theList.attrib['name'] == 'sharing':
                                    logger.debug(
                                        f"{macroInfo['name']} of type macro sharing: "
                                        f"{theList.text} in app {app}"
                                    )
                                    macroInfo["sharing"] = theList.text

                                    # If we are excluding private then check the sharing is
                                    # not user level (private in the GUI)
                                    if noPrivate and macroInfo["sharing"] == "user":
                                        logger.debug(
                                            f"{macroInfo['name']} of type macro found but "
                                            f"the noPrivate flag is true, excluding this "
                                            f"in app {app}"
                                        )
                                        keep = False
                                        break
                                    elif privateOnly and macroInfo["sharing"] != "user":
                                        logger.debug(
                                            f"{macroInfo['name']} of type macro found but "
                                            f"the privateOnly flag is true and sharing is "
                                            f"{macroInfo['sharing']}, excluding this in "
                                            f"app {app}"
                                        )
                                        keep = False
                                        break

                                    if (
                                        args.sharingFilter
                                        and not args.sharingFilter == macroInfo["sharing"]
                                    ):
                                        logger.debug(
                                            f"{macroInfo['name']} of type macro found but "
                                            f"the sharing level is set to {args.sharingFilter} "
                                            f"and this object has sharing {macroInfo['sharing']} "
                                            f"excluding this in app {app}"
                                        )
                                        keep = False
                                        break

                                elif theList.attrib['name'] == 'app':
                                    logger.debug(f"macro app: {theList.text}")
                                    foundApp = theList.text
                                    # We can see globally shared objects in our app context,
                                    # it does not mean we should migrate them as it's not ours...
                                    if app != foundApp:
                                        logger.debug(
                                            f"{macroInfo['name']} of type macro found in "
                                            f"app context of {app}, belongs to app context "
                                            f"{foundApp}, excluding it"
                                        )
                                        keep = False
                                        break
                                # owner is used as a nicer alternative to the author
                                elif theList.attrib['name'] == 'owner':
                                    macroInfo["owner"] = theList.text
                                    owner = theList.text
                                    logger.debug(
                                        f"{macroInfo['name']} of type macro owner is {owner}"
                                    )
                                    if includeOwner:
                                        if owner not in includeOwner:
                                            logger.debug(
                                                f"{macroInfo['name']} of type macro with "
                                                f"owner {owner} not in includeOwner list "
                                                f"in app {app}"
                                            )
                                            keep = False
                                            break
                                    if excludeOwner:
                                        if owner in excludeOwner:
                                            logger.debug(
                                                f"{macroInfo['name']} of type macro with "
                                                f"owner {owner} in excludeOwner list in "
                                                f"app {app}"
                                            )
                                            keep = False
                                            break
                        else:
                            # We have other attributes under content, we want the majority of them
                            attribName = theAttribute.attrib['name']
                            # Check if we have hit the disabled attribute and we have a noDisabled flag
                            if (
                                attribName == "disabled"
                                and noDisabled
                                and theAttribute.text == "1"
                            ):
                                logger.debug(
                                    f"noDisabled flag is true, {theAttribute.attrib['name']} "
                                    f"of type macro is disabled, excluded in app {app}"
                                )
                                keep = False
                                break
                            else:
                                # Otherwise we want this attribute
                                attribName = theAttribute.attrib['name']
                                # Some attributes do not work with the REST API or should not be migrated...
                                logger.debug(
                                    f"{macroInfo['name']} of type macro key/value pair of "
                                    f"{attribName}={theAttribute.text} in app {app}"
                                )
                                macroInfo[attribName] = theAttribute.text
            if keep:

                # Add this to the infoList
                sharing = macroInfo["sharing"]
                if sharing not in macros:
                    macros[sharing] = []
                macros[sharing].append(macroInfo)
                logger.info(
                    f"Recording macro info for {macroInfo['name']} in app {app} with "
                    f"owner {macroInfo['owner']} sharing level of {macroInfo['sharing']}"
                )

    app = destApp

    # Cycle through each one we need to migrate. We process global/app/user
    # as users can duplicate app level objects with the same names, but we create
    # everything at user level first then re-own it, so global/app must happen first.
    if "global" in macros:
        logger.debug(
            f"Now running macroCreation with knowledge objects of type macro with "
            f"global level sharing in app {app}"
        )
        macroCreation(
            macros["global"], destOwner, app, splunk_rest_dest,
            macroResults, override, overrideAlways
        )

    if "app" in macros:
        logger.debug(
            f"Now running macroCreation with knowledge objects of type macro with "
            f"app level sharing in app {app}"
        )
        macroCreation(
            macros["app"], destOwner, app, splunk_rest_dest,
            macroResults, override, overrideAlways
        )

    if "user" in macros:
        logger.debug(
            f"Now running macroCreation with knowledge objects of type macro with "
            f"user (private) level sharing in app {app}"
        )
        macroCreation(
            macros["user"], destOwner, app, splunk_rest_dest,
            macroResults, override, overrideAlways
        )

    return macroResults

###########################
#
# macroCreation
#   Runs the required queries to create the macro knowledge objects and then re-owns them to the correct user
#
###########################

def macroCreation(macros, destOwner, app, splunk_rest_dest, macroResults, override, overrideAlways):
    # Cache for users we've already checked/created
    checked_users = set()

    for aMacro in macros:
        sharing = aMacro["sharing"]
        name = aMacro["name"]
        owner = aMacro["owner"]
        curUpdated = aMacro["updated"]
        del aMacro["updated"]

        if destOwner:
            owner = destOwner

        # Check if owner exists on destination system and create if needed
        if owner not in checked_users and owner != 'nobody':
            # Extract host and port from splunk_rest_dest URL
            dest_parts = splunk_rest_dest.replace('https://', '').replace('http://', '').split(':')
            dest_host = dest_parts[0]
            dest_port = dest_parts[1] if len(dest_parts) > 1 else '8089'

            user_exists = check_and_create_user(
                dest_host, dest_port, args.destAuthtype,
                destUsername, destPassword, destToken, owner
            )

            if not user_exists:
                logger.error(f"Cannot create or verify user '{owner}' on destination system")
                appendToResults(macroResults, 'creationFailure', f"User creation failed for {name}")
                continue

            checked_users.add(owner)

        url = f"{splunk_rest_dest}/servicesNS/{owner}/{app}/properties/macros"

        encoded_name = urllib.parse.quote(name.encode('utf-8'))
        encoded_name = encoded_name.replace("/", "%2F")
        objURL = (
            f"{splunk_rest_dest}/servicesNS/-/{app}/configs/conf-macros/"
            f"{encoded_name}?output_mode=json"
        )
        # Verify=false is hardcoded to workaround local SSL issues
        res = make_request(
            objURL, method='get', auth_type=args.destAuthtype, username=destUsername,
            password=destPassword, token=args.destToken, verify=False
        )
        logger.debug(
            f"{name} of type macro checking on URL {objURL} to see if it exists"
        )
        objExists = False
        updated = None
        createdInAppContext = False
        # If we get 404 it definitely does not exist
        if (res.status_code == 404):
            logger.debug(f"URL {objURL} is throwing a 404, assuming new object creation")
        elif (res.status_code != requests.codes.ok):
            logger.error(
                f"URL {objURL} in app {app} status code {res.status_code} "
                f"reason {res.reason}, response: '{res.text}'"
            )
        else:
            # However the fact that we did not get a 404 does not mean it exists
            # in the context we expect it to, perhaps it's global and from another app context?
            # or perhaps it's app level but we're restoring a private object...
            logger.debug(f"Attempting to JSON loads on {res.text}")
            resDict = json.loads(res.text)
            for entry in resDict['entry']:
                sharingLevel = entry['acl']['sharing']
                appContext = entry['acl']['app']
                updatedStr = entry['updated']
                updated = determineTime(updatedStr, name, app, "macro")
                remoteObjOwner = entry['acl']['owner']
                logger.info(
                    f"sharing level {sharing}, app context {appContext}, "
                    f"remoteObjOwner {remoteObjOwner}, app {app}"
                )
                if appContext == app and (sharing == 'app' or sharing == 'global') and \
                   (sharingLevel == 'app' or sharingLevel == 'global'):
                    objExists = True
                    logger.debug(
                        f"name {name} of type macro in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                elif appContext == app and sharing == 'user' and \
                     sharingLevel == "user" and remoteObjOwner == owner:
                    objExists = True
                    logger.debug(
                        f"name {name} of type macro in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                elif appContext == app and sharingLevel == "user" and \
                     remoteObjOwner == owner and (sharing == "app" or sharing == "global"):
                    logger.debug(
                        f"name {name} of type macro in app context {app} found to "
                        f"exist on url {objURL} with sharing of {sharingLevel}, "
                        f"updated time of {updated}"
                    )
                else:
                    logger.debug(
                        f"name {name} of type macro in app context {app}, found the "
                        f"object with this name in sharingLevel {sharingLevel} and "
                        f"appContext {appContext}, updated time of {updated}, url {objURL}"
                    )

        createOrUpdate = None
        if objExists and not (override or overrideAlways):
            logger.info(
                f"{name} of type macro in app {app} on URL {objURL} exists, however "
                f"override/overrideAlways is not set so not changing this macro"
            )
            appendToResults(macroResults, 'creationSkip', objURL)
            continue
        elif objExists and override and not curUpdated > updated:
            logger.info(
                f"{name} of type macro in app {app} on URL {objURL} exists, "
                f"override is set but the source copy has modification time of "
                f"{curUpdated} destination has time of {updated}, skipping"
            )
            appendToResults(macroResults, 'creationSkip', objURL)
            continue
        elif objExists and overrideAlways:
            logger.info(
                f"{name} of type macro in app {app} on URL {objURL} exists, "
                f"overrideAlways is set, will update this macro"
            )

        if objExists:
            createOrUpdate = "update"
            url = url + "/" + encoded_name
        else:
            createOrUpdate = "create"

        logger.info(
            f"Attempting to {createOrUpdate} macro {name} on URL with name {url} "
            f"in app {app}"
        )

        payload = {"__stanza": name}
        # Create macro
        # I cannot seem to get this working on the /conf URL but this works so good enough,
        # and it's in the REST API manual...
        # servicesNS/-/search/properties/macros
        # __stanza = <name>
        macroCreationSuccessRes = False
        if not objExists:
            macroCreationSuccessRes = False
            res = make_request(
                url, method='post', auth_type=args.destAuthtype,
                username=destUsername, password=destPassword,
                token=args.destToken, data=payload, verify=False
            )
            if (res.status_code != requests.codes.ok and res.status_code != 201):
                logger.error(
                    f"{name} of type macro in app {app} with URL {url} status code "
                    f"{res.status_code} reason {res.reason}, response '{res.text}', "
                    f"owner {owner}"
                )
                appendToResults(macroResults, 'creationFailure', name)
                if res.status_code == 409:
                    # Delete the duplicate private object rather than leave it there for no purpose
                    url = url[:-4]
                    logger.warning(
                        f"Deleting the private object as it could not be re-owned "
                        f"{name} of type macro in app {app} with URL {url}"
                    )
                    make_request(
                        url, method='delete', auth_type=args.destAuthtype,
                        username=destUsername, password=destPassword,
                        token=args.destToken, verify=False
                    )
            else:
                # Macros always delete with the username in this URL context
                deletionURL = (
                    f"{splunk_rest_dest}/servicesNS/{owner}/{app}/configs/"
                    f"conf-macros/{name}"
                )
                logger.debug(
                    f"{name} of type macro in app {app} recording deletion URL as "
                    f"{deletionURL} with owner {owner}"
                )
                appendToResults(macroResults, 'creationSuccess', deletionURL)
                macroCreationSuccessRes = True

            logger.debug(
                f"{name} of type macro in app {app}, received response of: '{res.text}'"
            )

        payload = {}

        # Remove parts that cannot be posted to the REST API, sharing/owner we change later
        del aMacro["sharing"]
        del aMacro["name"]
        del aMacro["owner"]
        payload = aMacro

        if createOrUpdate == "create":
            url = url + "/" + encoded_name

        logger.debug(
            f"Attempting to modify macro {name} on URL {url} with payload '{payload}' "
            f"in app {app}"
        )
        res = make_request(
            url, method='post', auth_type=args.destAuthtype, username=destUsername,
            password=destPassword, token=args.destToken, data=payload, verify=False
        )
        if (res.status_code != requests.codes.ok and res.status_code != 201):
            logger.error(
                f"{name} of type macro in app {app} with URL {url} status code "
                f"{res.status_code} reason {res.reason}, response '{res.text}'"
            )

            if not objExists:
                appendToResults(macroResults, 'creationFailure', name)
                popLastResult(macroResults, 'macroCreationSuccess')
                logger.warning(
                    f"Deleting the private object as it could not be modified {name} "
                    f"of type macro in app {app} with URL {url}"
                )
                make_request(
                    url, method='delete', auth_type=args.destAuthtype,
                    username=destUsername, password=destPassword,
                    token=args.destToken, verify=False
                )
            else:
                appendToResults(macroResults, 'updateFailure', name)

            macroCreationSuccessRes = False
        else:
            if not createdInAppContext:
                # Re-owning it, I've switched URL's again here but it seems to be working
                # so will not change it
                url = (
                    f"{splunk_rest_dest}/servicesNS/{owner}/{app}/configs/"
                    f"conf-macros/{encoded_name}/acl"
                )
                payload = {"owner": owner, "sharing": sharing}
                logger.info(
                    f"Attempting to change ownership of macro {name} via URL {url} "
                    f"to owner {owner} in app {app} with sharing {sharing}"
                )
                res = make_request(
                    url, method='post', auth_type=args.destAuthtype,
                    username=destUsername, password=destPassword,
                    token=args.destToken, data=payload, verify=False
                )
                if (res.status_code != requests.codes.ok):
                    logger.error(
                        f"{name} of type macro in app {app} with URL {url} status "
                        f"code {res.status_code} reason {res.reason}, response "
                        f"'{res.text}', owner {owner} sharing level {sharing}"
                    )
                    # Hardcoded deletion URL as if this fails it should be this URL...
                    # (not parsing the XML here to confirm but this works fine)
                    deletionURL = (
                        f"{splunk_rest_dest}/servicesNS/{owner}/{app}/configs/"
                        f"conf-macros/{name}"
                    )
                    logger.info(
                        f"{name} of type macro in app {app} recording deletion URL "
                        f"as user URL due to change ownership failure {deletionURL}"
                    )
                    # Remove the old record
                    if not objExists:
                        popLastResult(macroResults, 'macroCreationSuccess')
                        macroCreationSuccessRes = False
                        url = url[:-4]
                        logger.warning(
                            f"Deleting the private object as it could not be modified "
                            f"{name} of type macro in app {app} with URL {url}"
                        )
                        make_request(
                            url, method='delete', auth_type=args.destAuthtype,
                            username=destUsername, password=destPassword,
                            token=args.destToken, verify=False
                        )
                        appendToResults(macroResults, 'creationFailure', name)
                    else:
                        appendToResults(macroResults, 'updateFailure', name)
                        macroCreationSuccessRes = False
                else:
                    macroCreationSuccessRes = True
                    logger.debug(
                        f"{name} of type macro in app {app}, ownership changed with "
                        f"response '{res.text}', new owner {owner} and sharing level {sharing}"
                    )
                    if objExists:
                        appendToResults(macroResults, 'updateSuccess', name)
            else:
                macroCreationSuccessRes = True
                appendToResults(macroResults, 'updateSuccess', name)

        if macroCreationSuccessRes:
            logger.info(
                f"{createOrUpdate} {name} of type macro in app {app} owner is {owner} "
                f"sharing level {sharing} was successful"
            )
        else:
            logger.warning(
                f"{createOrUpdate} {name} of type macro in app {app} owner is {owner} "
                f"sharing level {sharing} was not successful, a failure occurred"
            )
###########################
#
# Migration functions
#   These functions migrate the various knowledge objects mainly by calling the runQueries
#   with the appropriate options for that type
#   Excluding macros, they have their own function
#
###########################
###########################
#
# Dashboards
#
###########################
def dashboards(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [
        "disabled", "eai:appName", "eai:digest", "eai:userName", "isDashboard",
        "isVisible", "label", "rootNode", "description", "version"
    ]
    return runQueries(
        app, "/data/ui/views", "dashboard", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )
###########################
#
# Saved Searches
#
###########################
def savedsearches(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                  excludeEntities, includeOwner, excludeOwner, privateOnly,
                  ignoreVSID, disableAlertsOrReportsOnMigration, override,
                  overrideAlways, actionResults):
    ignoreList = ["embed.enabled", "triggered_alert_count"]

    # View states gracefully fail in the GUI, via the REST API you cannot create
    # the saved search if the view state does not exist. However, the same saved
    # search can work fine on an existing search head (even though the viewstate
    # has been deleted).
    if ignoreVSID:
        ignoreList.append("vsid")

    return runQueries(
        app, "/saved/searches", "savedsearches", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly,
        disableAlertsOrReportsOnMigration=disableAlertsOrReportsOnMigration,
        override=override, overrideAlways=overrideAlways,
        actionResults=actionResults
    )
###########################
#
# field definitions
#
###########################
def calcfields(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
               excludeEntities, includeOwner, excludeOwner, privateOnly,
               override, overrideAlways, actionResults):
    ignoreList = ["attribute", "type"]
    aliasAttributes = {"field.name": "name"}
    return runQueries(
        app, "/data/props/calcfields", "calcfields", ignoreList, destApp,
        aliasAttributes, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

def fieldaliases(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                 excludeEntities, includeOwner, excludeOwner, privateOnly,
                 override, overrideAlways, actionResults):
    ignoreList = ["attribute", "type", "value"]
    return runQueries(
        app, "/data/props/fieldaliases", "fieldaliases", ignoreList, destApp,
        nameOverride="name", destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

def fieldextractions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                     excludeEntities, includeOwner, excludeOwner, privateOnly,
                     override, overrideAlways, actionResults):
    ignoreList = ["attribute"]
    return runQueries(
        app, "/data/props/extractions", "fieldextractions", ignoreList,
        destApp, {}, {"Inline": "EXTRACT", "Uses transform": "REPORT"},
        "attribute", destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

def fieldtransformations(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                         excludeEntities, includeOwner, excludeOwner, privateOnly,
                         override, overrideAlways, actionResults):
    ignoreList = [
        "attribute", "DEFAULT_VALUE", "DEPTH_LIMIT", "LOOKAHEAD", "MATCH_LIMIT",
        "WRITE_META", "eai:appName", "eai:userName", "DEST_KEY"
    ]
    return runQueries(
        app, "/data/transforms/extractions", "fieldtransformations",
        ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

def workflowactions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                    excludeEntities, includeOwner, excludeOwner, privateOnly,
                    override, overrideAlways, actionResults):
    ignoreList = ["disabled", "eai:appName", "eai:userName"]
    return runQueries(
        app, "/data/ui/workflow-actions", "workflow-actions", ignoreList,
        destApp, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

def sourcetyperenaming(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                       excludeEntities, includeOwner, excludeOwner, privateOnly,
                       override, overrideAlways, actionResults):
    ignoreList = ["attribute", "disabled", "eai:appName", "eai:userName", "stanza", "type"]
    return runQueries(
        app, "/data/props/sourcetype-rename", "sourcetype-rename", ignoreList,
        destApp, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# tags
#
##########################
def tags(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
         excludeEntities, includeOwner, excludeOwner, privateOnly, override,
         overrideAlways, actionResults):
    ignoreList = ["disabled", "eai:appName", "eai:userName"]
    return runQueries(
        app, "/configs/conf-tags", "tags", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# eventtypes
#
##########################
def eventtypes(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
               excludeEntities, includeOwner, excludeOwner, privateOnly,
               override, overrideAlways, actionResults):
    ignoreList = ["disabled", "eai:appName", "eai:userName"]
    return runQueries(
        app, "/saved/eventtypes", "eventtypes", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# navMenus
#
##########################
def navMenu(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
            excludeEntities, includeOwner, excludeOwner, privateOnly, override,
            overrideAlways, actionResults):
    ignoreList = ["disabled", "eai:appName", "eai:userName", "eai:digest", "rootNode"]
    # If override we override the default nav menu of the destination app
    return runQueries(
        app, "/data/ui/nav", "navMenu", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        override=override, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# data models
#
##########################
def datamodels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
               excludeEntities, includeOwner, excludeOwner, privateOnly, override,
               overrideAlways, actionResults):
    ignoreList = [
        "disabled", "eai:appName", "eai:userName", "eai:digest", "eai:type",
        "acceleration.allowed"
    ]
    # If override we override the default nav menu of the destination app
    return runQueries(
        app, "/datamodel/model", "datamodels", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# collections
#
##########################
def collections(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                excludeEntities, includeOwner, excludeOwner, privateOnly, override,
                overrideAlways, actionResults):
    ignoreList = ["eai:appName", "eai:userName", "type"]
    # nobody is the only username that can be used when working with collections
    return runQueries(
        app, "/storage/collections/config", "collections (kvstore definition)",
        ignoreList, destApp, destOwner="nobody", noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )
###########################
#
# viewstates
#
##########################
def viewstates(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
               excludeEntities, includeOwner, excludeOwner, privateOnly, override,
               overrideAlways, actionResults):
    ignoreList = ["eai:appName", "eai:userName"]
    # nobody is the only username that can be used when working with collections
    return runQueries(
        app, "/configs/conf-viewstates", "viewstates", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# time labels (conf-times)
#
##########################
def times(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
          excludeEntities, includeOwner, excludeOwner, privateOnly, override,
          overrideAlways, actionResults):
    ignoreList = ["disabled", "eai:appName", "eai:userName", "header_label"]
    return runQueries(
        app, "/configs/conf-times", "times (conf-times)", ignoreList, destApp,
        destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled,
        includeEntities=includeEntities, excludeEntities=excludeEntities,
        includeOwner=includeOwner, excludeOwner=excludeOwner,
        privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# panels
#
##########################
def panels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
           excludeEntities, includeOwner, excludeOwner, privateOnly, override,
           overrideAlways, actionResults):
    ignoreList = [
        "disabled", "eai:digest", "panel.title", "rootNode", "eai:appName",
        "eai:userName"
    ]
    return runQueries(
        app, "/data/ui/panels", "pre-built dashboard panels", ignoreList,
        destApp, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# lookups (definition/automatic)
#
##########################
def lookupDefinitions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                      excludeEntities, includeOwner, excludeOwner, privateOnly,
                      override, overrideAlways, actionResults):
    ignoreList = [
        "disabled", "eai:appName", "eai:userName", "CAN_OPTIMIZE", "CLEAN_KEYS",
        "DEPTH_LIMIT", "KEEP_EMPTY_VALS", "LOOKAHEAD", "MATCH_LIMIT", "MV_ADD",
        "SOURCE_KEY", "WRITE_META", "fields_array", "type"
    ]
    # If override we override the default nav menu of the destination app
    return runQueries(
        app, "/data/transforms/lookups", "lookup definition", ignoreList,
        destApp, destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )


def automaticLookups(app, destApp, destOwner, noPrivate, noDisabled, includeEntities,
                     excludeEntities, includeOwner, excludeOwner, privateOnly,
                     override, overrideAlways, actionResults):
    ignoreList = ["attribute", "type", "value"]
    return runQueries(
        app, "/data/props/lookups", "automatic lookup", ignoreList, destApp,
        {}, {}, "attribute", destOwner=destOwner, noPrivate=noPrivate,
        noDisabled=noDisabled, includeEntities=includeEntities,
        excludeEntities=excludeEntities, includeOwner=includeOwner,
        excludeOwner=excludeOwner, privateOnly=privateOnly, override=override,
        overrideAlways=overrideAlways, actionResults=actionResults
    )

###########################
#
# Logging functions for the output we provide at the end of a migration run
#
##########################

def logDeletionScriptInLogs(theDict, printPasswords, destUsername, destPassword):
    list_to_log = None

    # If we have nothing to log, return
    if not theDict:
        return
    if 'creationSuccess' in theDict:
        list_to_log = theDict['creationSuccess']
    else:
        return

    for item in list_to_log:
        item = item.replace("(", "\(").replace(")", "\)")
        if printPasswords:
            logger.info(
                f"curl -k -u {destUsername}:{destPassword} --request DELETE {item}"
            )
        else:
            logger.info(f"curl -k --request DELETE {item}")


def logCreationFailure(theDict, obj_type, app):
    list_to_log = None
    # If we have nothing to log, return
    if not theDict:
        return
    if 'creationFailure' in theDict:
        list_to_log = theDict['creationFailure']
    else:
        return

    for item in list_to_log:
        logger.warning(f"In App {app}, {obj_type} '{item}' failed to create")


def logStats(resultsDict, obj_type, app):
    successList = []
    failureList = []
    skippedList = []
    updateSuccess = []
    updateFailure = []

    # If we have nothing to log, return
    if not resultsDict:
        return

    if 'creationSuccess' in resultsDict:
        successList = resultsDict['creationSuccess']
    if 'creationFailure' in resultsDict:
        failureList = resultsDict['creationFailure']
    if 'creationSkip' in resultsDict:
        skippedList = resultsDict['creationSkip']
    if 'updateSuccess' in resultsDict:
        updateSuccess = resultsDict['updateSuccess']
    if 'updateFailure' in resultsDict:
        updateFailure = resultsDict['updateFailure']

    logger.info(
        f"App {app}, {len(successList)} {obj_type} successfully migrated "
        f"{len(failureList)} {obj_type} failed to migrate, "
        f"{len(skippedList)} were skipped due to existing already, "
        f"{len(updateSuccess)} were updated, {len(updateFailure)} failed to update"
    )


def handleFailureLogging(failureList, obj_type, app):
    logCreationFailure(failureList, obj_type, app)


def logDeletion(successList, printPasswords, destUsername, destPassword):
    logDeletionScriptInLogs(successList, printPasswords, destUsername, destPassword)


# Helper function as per https://stackoverflow.com/questions/31433989/return-copy-of-dictionary-excluding-specified-keys
def without_keys(d, keys):
    return {x: d[x] for x in d if x not in keys}

###########################
#
# Success / Failure lists
#   these are used later in the code to print what worked, what failed et cetera
#
##########################
savedSearchResults = None
calcfieldsResults = None
fieldaliasesResults = None
fieldextractionsResults = None
dashboardResults = None
fieldTransformationsResults = None
timesResults = None
panelsResults = None
workflowActionsResults = None
sourcetypeRenamingResults = None
tagsResults = None
eventtypeResults = None
navMenuResults = None
datamodelResults = None
lookupDefinitionsResults = None
automaticLookupsResults = None
collectionsResults = None
viewstatesResults = None
macroResults = None

# If the all switch is provided, migrate everything
if args.all:
    args.macros = True
    args.tags = True
    args.allFieldRelated = True
    args.lookupDefinition = True
    args.automaticLookup = True
    args.datamodels = True
    args.dashboards = True
    args.savedsearches = True
    args.navMenu = True
    args.eventtypes = True
    args.collections = True
    args.times = True
    args.panels = True
    args.viewstates = True

# All field related switches on anything under Settings -> Fields
if args.allFieldRelated:
    args.calcFields = True
    args.fieldAlias = True
    args.fieldExtraction = True
    args.fieldTransforms = True
    args.workflowActions = True
    args.sourcetypeRenaming = True

###########################
#
# Include/Exclude lists
#   we have the option of allowing only particular entities, or excluding some entities
#   we also do the same trick for owners so we can include only some users
#   or exclude some users from migration
#
##########################
includeEntities = None
if args.includeEntities:
    includeEntities = [x.strip() for x in args.includeEntities.split(',')]

excludeEntities = None
if args.excludeEntities:
    excludeEntities = [x.strip() for x in args.excludeEntities.split(',')]

excludeOwner = None
if args.excludeOwner:
    excludeOwner = [x.strip() for x in args.excludeOwner.split(',')]

includeOwner = None
if args.includeOwner:
    includeOwner = [x.strip() for x in args.includeOwner.split(',')]

excludedList = ["srcPassword", "destPassword"]
cleanArgs = without_keys(vars(args), excludedList)
logger.info(f"transfer splunk knowledge objects run with arguments {cleanArgs}")

src_app_list = None
# Wildcarded app names...use regex
if srcApp.find("*") != -1:
    src_app_list = []
    url = (
        f"{splunk_rest}/services/apps/local?search=disabled%3D0&f=title&count=0"
        f"&output_mode=json"
    )
    app_pattern = re.compile(srcApp)
    # Verify=false is hardcoded to workaround local SSL issues
    res = make_request(
        url, method='get', auth_type=args.srcAuthtype, username=srcUsername,
        password=srcPassword, token=args.srcToken, verify=False
    )
    resDict = json.loads(res.text)
    for entry in resDict['entry']:
        logger.debug(f"entry name is {entry['name']}, pattern is {srcApp}")
        if app_pattern.search(entry['name']):
            logger.info(f"Adding app {entry['name']} to the list of apps")
            src_app_list.append(entry['name'])

###########################
#
# Run the required functions based on the args
#   Based on the command line parameters actually run the functions which will migrate the knowledge objects
#
##########################
# Check and create destination app before starting transfers
logger.info(f"Checking if destination app '{destApp}' exists")
dest_parts = splunk_rest_dest.replace('https://', '').replace('http://', '').split(':')
dest_host = dest_parts[0]
dest_port = dest_parts[1] if len(dest_parts) > 1 else '8089'

# Get source app ACL first
src_parts = splunk_rest.replace('https://', '').replace('http://', '').split(':')
src_host = src_parts[0]
src_port = src_parts[1] if len(src_parts) > 1 else '8089'

source_app_acl = get_app_acl(
    src_host, src_port, args.srcAuthtype,
    srcUsername, srcPassword, srcToken, srcApp
)

if source_app_acl:
    logger.info(
        f"Retrieved source app '{srcApp}' ACL - sharing: "
        f"{source_app_acl.get('sharing')}, owner: {source_app_acl.get('owner')}"
    )

app_exists = check_and_create_app(
    dest_host, dest_port, args.destAuthtype,
    destUsername, destPassword, destToken, destApp, source_app_acl
)

if not app_exists:
    logger.error(f"Cannot create or verify destination app '{destApp}' - exiting")
    exit(1)

logger.info(f"Destination app '{destApp}' is ready for knowledge object transfers")

if args.macros:
    logger.info("Begin macros transfer")
    macroResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            macros(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, macroResults
            )
    else:
        macros(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            macroResults
        )
    logger.info("End macros transfer")

if args.tags:
    logger.info("Begin tags transfer")
    tagsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            tags(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, tagsResults
            )
    else:
        tagsResults = tags(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            tagsResults
        )
    logger.info("End tags transfer")

if args.eventtypes:
    logger.info("Begin eventtypes transfer")
    eventTypesResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            eventtypes(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, eventTypesResults
            )
    else:
        eventtypes(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            eventTypesResults
        )
    logger.info("End eventtypes transfer")

if args.calcFields:
    logger.info("Begin calcFields transfer")
    calcfieldsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            calcfields(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, calcfieldsResults
            )
    else:
        calcfields(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            calcfieldsResults
        )
    logger.info("End calcFields transfer")

if args.fieldAlias:
    logger.info("Begin fieldAlias transfer")
    fieldaliasesResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            fieldaliases(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, fieldaliasesResults
            )
    else:
        fieldaliases(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            fieldaliasesResults
        )
    logger.info("End fieldAlias transfer")

if args.fieldTransforms:
    logger.info("Begin fieldTransforms transfer")
    fieldTransformationsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            fieldtransformations(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, fieldTransformationsResults
            )
    else:
        fieldTransformationsResults = fieldtransformations(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            fieldTransformationsResults
        )
    logger.info("End fieldTransforms transfer")

if args.fieldExtraction:
    logger.info("Begin fieldExtraction transfer")
    fieldextractionsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            fieldextractions(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, fieldextractionsResults
            )
    else:
        fieldextractions(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            fieldextractionsResults
        )
    logger.info("End fieldExtraction transfer")

if args.collections:
    logger.info("Begin collections (kvstore definition) transfer")
    collectionsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            collections(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, collectionsResults
            )
    else:
        collections(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            collectionsResults
        )
    logger.info("End collections (kvstore definition) transfer")

if args.lookupDefinition:
    logger.info("Begin lookupDefinitions transfer")
    lookupDefinitionsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            lookupDefinitions(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, lookupDefinitionsResults
            )
    else:
        lookupDefinitions(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            lookupDefinitionsResults
        )
    logger.info("End lookupDefinitions transfer")

if args.automaticLookup:
    logger.info("Begin automaticLookup transfer")
    automaticLookupsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            automaticLookups(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, automaticLookupsResults
            )
    else:
        automaticLookups(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            automaticLookupsResults
        )
    logger.info("End automaticLookup transfer")

if args.times:
    logger.info("Begin times (conf-times) transfer")
    timesResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            times(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, timesResults
            )
    else:
        times(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            timesResults
        )
    logger.info("End times (conf-times) transfer")

if args.viewstates:
    logger.info("Begin viewstates transfer")
    viewstatesResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            viewstates(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, viewstatesResults
            )
    else:
        viewstates(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            viewstatesResults
        )
    logger.info("End viewstates transfer")

if args.panels:
    logger.info("Begin pre-built dashboard panels transfer")
    panelsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            panels(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, panelsResults
            )
    else:
        panels(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            panelsResults
        )
    logger.info("End pre-built dashboard panels transfer")

if args.datamodels:
    logger.info("Begin datamodels transfer")
    datamodelResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            datamodels(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, datamodelResults
            )
    else:
        datamodels(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            datamodelResults
        )
    logger.info("End datamodels transfer")

if args.dashboards:
    logger.info("Begin dashboards transfer")
    dashboardResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            dashboards(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, dashboardResults
            )
    else:
        dashboards(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            dashboardResults
        )
    logger.info("End dashboards transfer")

if args.savedsearches:
    logger.info("Begin savedsearches transfer")
    savedSearchResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            savedsearches(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.ignoreViewstatesAttribute,
                args.disableAlertsOrReportsOnMigration, args.overrideMode,
                args.overrideAlwaysMode, savedSearchResults
            )
    else:
        savedsearches(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.ignoreViewstatesAttribute,
            args.disableAlertsOrReportsOnMigration, args.overrideMode,
            args.overrideAlwaysMode, savedSearchResults
        )
    logger.info("End savedsearches transfer")

if args.workflowActions:
    logger.info("Begin workflowActions transfer")
    workflowActionsResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            workflowactions(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, workflowActionsResults
            )
    else:
        workflowactions(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            workflowActionsResults
        )
    logger.info("End workflowActions transfer")

if args.sourcetypeRenaming:
    logger.info("Begin sourcetypeRenaming transfer")
    sourcetypeRenamingResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            sourcetyperenaming(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, sourcetypeRenamingResults
            )
    else:
        sourcetyperenaming(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            sourcetypeRenamingResults
        )
    logger.info("End sourcetypeRenaming transfer")

if args.navMenu:
    logger.info("Begin navMenu transfer")
    navMenuResults = {}
    if src_app_list:
        for current_src_app in src_app_list:
            navMenu(
                current_src_app, destApp, destOwner, args.noPrivate,
                args.noDisabled, includeEntities, excludeEntities, includeOwner,
                excludeOwner, args.privateOnly, args.overrideMode,
                args.overrideAlwaysMode, navMenuResults
            )
    else:
        navMenu(
            srcApp, destApp, destOwner, args.noPrivate, args.noDisabled,
            includeEntities, excludeEntities, includeOwner, excludeOwner,
            args.privateOnly, args.overrideMode, args.overrideAlwaysMode,
            navMenuResults
        )
    logger.info("End navMenu transfer")

###########################
#
# Logging
#   At the end of all the migration work we log a list of curl commands to "undo" what was migrated
#   we also log failures and stats around the number of successful/failed migrations et cetera
#
##########################
logDeletion(macroResults, args.printPasswords, destUsername, destPassword)
logDeletion(tagsResults, args.printPasswords, destUsername, destPassword)
logDeletion(eventtypeResults, args.printPasswords, destUsername, destPassword)
logDeletion(calcfieldsResults, args.printPasswords, destUsername, destPassword)
logDeletion(fieldaliasesResults, args.printPasswords, destUsername, destPassword)
logDeletion(fieldextractionsResults, args.printPasswords, destUsername, destPassword)
logDeletion(fieldTransformationsResults, args.printPasswords, destUsername, destPassword)
logDeletion(lookupDefinitionsResults, args.printPasswords, destUsername, destPassword)
logDeletion(automaticLookupsResults, args.printPasswords, destUsername, destPassword)
logDeletion(viewstatesResults, args.printPasswords, destUsername, destPassword)
logDeletion(datamodelResults, args.printPasswords, destUsername, destPassword)
logDeletion(dashboardResults, args.printPasswords, destUsername, destPassword)
logDeletion(savedSearchResults, args.printPasswords, destUsername, destPassword)
logDeletion(workflowActionsResults, args.printPasswords, destUsername, destPassword)
logDeletion(sourcetypeRenamingResults, args.printPasswords, destUsername, destPassword)
logDeletion(navMenuResults, args.printPasswords, destUsername, destPassword)
logDeletion(collectionsResults, args.printPasswords, destUsername, destPassword)
logDeletion(timesResults, args.printPasswords, destUsername, destPassword)
logDeletion(panelsResults, args.printPasswords, destUsername, destPassword)

handleFailureLogging(macroResults, "macros", srcApp)
handleFailureLogging(tagsResults, "tags", srcApp)
handleFailureLogging(eventtypeResults, "eventtypes", srcApp)
handleFailureLogging(calcfieldsResults, "calcfields", srcApp)
handleFailureLogging(fieldaliasesResults, "fieldaliases", srcApp)
handleFailureLogging(fieldextractionsResults, "fieldextractions", srcApp)
handleFailureLogging(fieldTransformationsResults, "fieldtransformations", srcApp)
handleFailureLogging(lookupDefinitionsResults, "lookupdef", srcApp)
handleFailureLogging(automaticLookupsResults, "automatic lookup", srcApp)
handleFailureLogging(viewstatesResults, "viewstates", srcApp)
handleFailureLogging(datamodelResults, "datamodels", srcApp)
handleFailureLogging(dashboardResults, "dashboard", srcApp)
handleFailureLogging(savedSearchResults, "savedsearch", srcApp)
handleFailureLogging(workflowActionsResults, "workflowactions", srcApp)
handleFailureLogging(sourcetypeRenamingResults, "sourcetype-renaming", srcApp)
handleFailureLogging(navMenuResults, "navMenu", srcApp)
handleFailureLogging(collectionsResults, "collections", srcApp)
handleFailureLogging(timesResults, "collections", srcApp)
handleFailureLogging(panelsResults, "collections", srcApp)

logStats(macroResults, "macros", srcApp)
logStats(tagsResults, "tags", srcApp)
logStats(eventtypeResults, "eventtypes", srcApp)
logStats(calcfieldsResults, "calcfields", srcApp)
logStats(fieldaliasesResults, "fieldaliases", srcApp)
logStats(fieldextractionsResults, "fieldextractions", srcApp)
logStats(fieldTransformationsResults, "fieldtransformations", srcApp)
logStats(lookupDefinitionsResults, "lookupdef", srcApp)
logStats(automaticLookupsResults, "automatic lookup", srcApp)
logStats(viewstatesResults, "viewstates", srcApp)
logStats(datamodelResults, "datamodels", srcApp)
logStats(dashboardResults, "dashboard", srcApp)
logStats(savedSearchResults, "savedsearch", srcApp)
logStats(workflowActionsResults, "workflowactions", srcApp)
logStats(sourcetypeRenamingResults, "sourcetype-renaming", srcApp)
logStats(navMenuResults, "navMenu", srcApp)
logStats(collectionsResults, "collections", srcApp)
logStats(timesResults, "times (conf-times)", srcApp)
logStats(panelsResults, "pre-built dashboard panels", srcApp)

logger.info("The undo command is: grep -o \"curl.*DELETE.*\" /tmp/transfer_knowledgeobj.log "
            "| grep -v \"curl\.\*DELETE\"")
logger.info("Done")
