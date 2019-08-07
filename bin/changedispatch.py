#!/usr/bin/env python
from __future__ import absolute_import, division, print_function, unicode_literals
import sys
import os
import json
import utility
import re
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from splunklib.searchcommands import dispatch, GeneratingCommand, Configuration, Option
from splunklib.binding import HTTPError

import logging
import splunk

def setup_logging():
    """
     setup_logging as found on http://dev.splunk.com/view/logging/SP-CAAAFCN
    """
    logger = logging.getLogger('splunk.shareprivateobjects')
    SPLUNK_HOME = os.environ['SPLUNK_HOME']

    LOGGING_DEFAULT_CONFIG_FILE = os.path.join(SPLUNK_HOME, 'etc', 'log.cfg')
    LOGGING_LOCAL_CONFIG_FILE = os.path.join(SPLUNK_HOME, 'etc', 'log-local.cfg')
    LOGGING_STANZA_NAME = 'python'
    LOGGING_FILE_NAME = "changedispatch.log"
    BASE_LOG_PATH = os.path.join('var', 'log', 'splunk')
    LOGGING_FORMAT = "%(asctime)s %(levelname)-s\t %(message)s"
    splunk_log_handler = logging.handlers.RotatingFileHandler(os.path.join(SPLUNK_HOME, BASE_LOG_PATH, LOGGING_FILE_NAME), mode='a')
    splunk_log_handler.setFormatter(logging.Formatter(LOGGING_FORMAT))
    logger.addHandler(splunk_log_handler)
    splunk.setupSplunkLogger(logger, LOGGING_DEFAULT_CONFIG_FILE, LOGGING_LOCAL_CONFIG_FILE, LOGGING_STANZA_NAME)
    return logger
logger = setup_logging()

@Configuration(type='reporting')
class ChangeDispatchCommand(GeneratingCommand):

    appname = Option(require=True)
    newttl = Option(require=True)
    savedsearch = Option(require=True)
    owner = Option(require=False)
    sharing = Option(require=False)

    def generate(self):
        """
          The logic is:
            If the requested savedsearch is owned by the current user, or the requesting user is an admin user, then
            change the dispatch.ttl value of the saved search to the requested newttl value passed in
            If the optional sharing level is not specified check for the savedsearch in the private / user context first
            then app context
            If the owner is specified look under the particular owner context, only someone with admin access can use this option
        """
        (username, roles) = utility.determine_username(self.service)

        if self.owner is not None:
            if not 'admin' in roles:
                yield {'result': 'The owner passed in was %s but the user %s does not have the admin role. You may only change the TTL of your own saved searches' % (self.owner, username) }
                return
        
        if self.sharing is not None and self.sharing != 'user':
            context = 'nobody'
        else:
            if self.owner is not None:
                context = self.owner
            else:
                context = username

        url = 'https://localhost:8089/servicesNS/%s/%s/' % (context, self.appname)
        url = url + 'saved/searches/' + self.savedsearch + '?output_mode=json'

        headers = { 'Authorization': 'Splunk ' + self._metadata.searchinfo.session_key }
        #Hardcoded user credentials
        attempt = requests.get(url, verify=False, headers=headers)
        if attempt.status_code != 200:
            yield {'result': 'Unknown failure, received a non-200 response code of %s on the URL %s, text result is %s' % (attempt.status_code, url, attempt.text)}
            return
            
        #We received a response but we now need the details on owner, sharing level and the app context it came from 
        acl = json.loads(attempt.text)['entry'][0]['acl']
        obj_sharing = acl['sharing']
        obj_owner = acl['owner']
        obj_app = acl['app']

        #We advised an explicit sharing level, but the saved search we found does not match the expected level
        if self.sharing is not None and obj_sharing != self.sharing:
            yield {'result': 'Object found but sharing level is %s, expected sharing level of %s, not changing dispatch.ttl in app %s' % (obj_sharing, self.sharing, self.appname) }
            return
         
        #Does the owner of the object match the person logged in?
        if obj_owner != username:
            #admins are allowed to change dispatch.ttl of any saved search they wish
            if not 'admin' in roles:
                yield {'result': 'Object found but owner is %s, user is %s, not changing dispatch.ttl in app %s as you do not have the admin role' % (obj_owner, username, self.appname) }
                return

        #Make sure the dispatch.ttl value looks valid
        if not re.match(r"^[0-9]+p?$", self.newttl):
            yield {'result': 'The requested new TTL value of %s did not match the regular expression, ttl values are in seconds and can end in a p for execution period.' % (self.newttl) }
            return

        #If the saved search is currently app level we must use the nobody context or we create an empty saved search in private context...
        if obj_sharing != 'user':
            #A globally shared object may appear to be in this app but could be from a different app
            #we must post the correct app context otherwise we create an empty saved search with a modified dispatch.ttl in the wrong app
            #To make it simple for the user we do this for them rather than request them to use the correct app name...
            if obj_app != self.appname:
                context = 'nobody/' + obj_app
            else:
                context = 'nobody/' + self.appname
        else:
            context = obj_owner + '/' + self.appname

        #At this point we have run our checks so are happy to change the dispatch.ttl value
        data = { 'dispatch.ttl': self.newttl, 'output_mode': 'json' }
        url = 'https://localhost:8089/servicesNS/%s/' % (context)
        url = url + 'saved/searches/' + self.savedsearch

        #Hardcoded user credentials
        attempt = requests.post(url, verify=False, data=data, headers=headers)
        if attempt.status_code != 200:
            yield {'result': 'Unknown failure, received a non-200 response code of %s on the URL %s, text result is %s' % (attempt.status_code, url, attempt.text)}
            return
        else:
            logger.info("app=%s savedsearch='%s' owner=%s has had the TTL value changed to newttl=%s via url='%s' sharing_arg=%s owner_arg=%s username=%s" % (self.appname, self.savedsearch, obj_owner, self.newttl, url, self.sharing, self.owner, username))
            ttl = json.loads(attempt.text)['entry'][0]['content']['dispatch.ttl']
            yield {'result': 'TTL updated, new value is showing as %s for saved search %s in app %s with owner %s' % (ttl, self.savedsearch, self.appname, obj_owner) }

dispatch(ChangeDispatchCommand, sys.argv, sys.stdin, sys.stdout, __name__)
