import requests
import logging
from logging.config import dictConfig
import urllib
import argparse
import json

###########################
#
# Disable Splunk Object script
#   Scripted disable/enable of Splunk objects, at this stage saved searches (reports & alerts)
# 
###########################

#Setup the logging, the plan was to default to INFO and change to DEBUG level but it's currently the
#opposite version of this
logging_config = dict(
    version = 1,
    formatters = {
        'f': {'format':
              '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'}
        },
    handlers = {
        'h': {'class': 'logging.StreamHandler',
              'formatter': 'f',
              'level': logging.DEBUG},
        'file': {'class' : 'logging.handlers.RotatingFileHandler',
              'filename' : '/tmp/disable_splunk_objects.log',
              'formatter': 'f',
              'maxBytes' :  2097152,
              'level': logging.DEBUG}
        },        
    root = {
        'handlers': ['h','file'],
        'level': logging.DEBUG,
        },
)

dictConfig(logging_config)

logger = logging.getLogger()

#Create the argument parser
parser = argparse.ArgumentParser(description='Migrate Splunk configuration from 1 Splunk search head to another Splunk search head via the REST API')
parser.add_argument('-destURL', help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example', required=True)
parser.add_argument('-destApp', help='Application name to be used on destURL to be migrated to defaults to srcApp', required=True)
parser.add_argument('-destUsername', help='username to use for REST API of destURL argument', required=True)
parser.add_argument('-destPassword', help='password to use for REST API of destURL argument', required=True)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-printPasswords', help='(optional) print passwords in the log files (dev only)', action='store_true')
parser.add_argument('-includeEntities', help='(optional) comma separated list of object values to include (double quoted), if this option is used only this list of objects will be changed')
parser.add_argument('-excludeEntities', help='(optional) comma separated list of object values to exclude (double quoted), if this option is used all other objects within the app may be disabled or enabled')
parser.add_argument('-relativeDate', help='(optional) Splunk-style entry for anything modified after -1d@d is in scope, or -5d or similar')
parser.add_argument('-absoluteDate', help='(optional) Anything modified after, for example 2018-01-01 12:00:00, must be in YYYY-MM-DD HH:MM:SS format, note this will use the timezone of the destUsername in terms of timezone offset')
parser.add_argument('-enableObj', help='(optional) Run the "enable" option against scheduled searches/reports', action='store_true')
parser.add_argument('-disableObj', help='(optional) Run the "disable" optoin against scheduled searches/reports', action='store_true')

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)
    
#helper function as per https://stackoverflow.com/questions/31433989/return-copy-of-dictionary-excluding-specified-keys
def without_keys(d, keys):
    return {x: d[x] for x in d if x not in keys}

excludedList = [ "destPassword" ]
cleanArgs = without_keys(vars(args), excludedList)
logger.info("disableSplunkObjects run with arguments %s" % (cleanArgs))

includeEntities = None
if args.includeEntities:
    includeEntities = [x.strip() for x in args.includeEntities.split(',')]

excludeEntities = None
if args.excludeEntities:
    excludeEntities = [x.strip() for x in args.excludeEntities.split(',')]

logger.info("Running a query to obtain a list of objects in scope" % (cleanArgs))

if args.absoluteDate:
    search = "| rest \"/servicesNS/-/" + args.destApp + "/directory?count=-1\" splunk_server=local" \
                "| search eai:acl.app=" + args.destApp + " eai:type=\"savedsearch\"" \
                "| eval updatedepoch=strptime(updated, \"%Y-%m-%dT%H:%M:%S%:z\")" \
                "| where updatedepoch>strptime(\"" + args.absoluteDate + "\", \"%Y-%m-%d %H:%M:%S\")" \
                "| table title, eai:acl.sharing, eai:acl.owner, updated"
elif args.relativeDate:
    search = "| rest \"/servicesNS/-/" + args.destApp + "/directory?count=-1\" splunk_server=local" \
                "| search eai:acl.app=" + args.destApp + " eai:type=\"savedsearch\"" \
                "| eval updatedepoch=strptime(updated, \"%Y-%m-%dT%H:%M:%S%:z\")" \
                "| where updatedepoch>relative_time(now(), \"" + args.relativeDate + "\")" \
                "| table title, eai:acl.sharing, eai:acl.owner, updated"
else:
    search = "| rest \"/servicesNS/-/" + args.destApp + "/directory?count=-1\" splunk_server=local" \
                "| search eai:acl.app=" + args.destApp + " eai:type=\"savedsearch\"" \
                "| table title, eai:acl.sharing, eai:acl.owner, updated"

payload = { "search": search, "output_mode": "json", "exec_mode" : "oneshot" }
url = args.destURL + "/services/search/jobs"

logging.debug("Sending request to %s with username %s, payload %s" % (url, args.destUsername, payload))

res = requests.post(url, auth=(args.destUsername,args.destPassword), verify=False, data=payload)
if (res.status_code != requests.codes.ok and res.status_code != 201):
    logger.error("URL %s status code %s reason %s, response '%s', in app %s" % (url, res.status_code, res.reason, res.text, args.destApp))
else:
    logger.debug("App %s with URL %s result is: '%s'" % (args.destApp, url, res.text))

#load the result
jsonRes = json.loads(res.text)
resList = jsonRes["results"]
logging.debug("Received %s results" % (len(resList)))

for aRes in resList:
    name = aRes["title"]
    sharing = aRes["eai:acl.sharing"]
    owner = aRes["eai:acl.owner"]
    lastUpdated = aRes["updated"]
    
    logging.debug("Working with name %s sharing %s owner %s updated %s in app %s" % (name, sharing, owner, lastUpdated, args.destApp))

    if includeEntities:
        if not name in includeEntities:
            logger.debug("%s not in includeEntities list in app %s therefore skipping" % (name, args.destApp))
            continue
    if excludeEntities:
        if name in excludeEntities:
            logger.debug("%s in excludeEntities list in app %s therefore skipping" % (name, args.destApp))
            keep = False
            continue
    
    isAlert = False
    #If we may have to take action then we need to know if it's an alert or a report
    #as this is a slightly different change
    if args.enableObj or args.disableObj:
        if sharing == "user":
            url = args.destURL + "/servicesNS/" + owner + "/" + args.destApp + "/saved/searches/" + name
        else:
            url = args.destURL + "/servicesNS/nobody/" + args.destApp + "/saved/searches/" + name
        
        payload = { "output_mode": "json" }
        
        logging.debug("Sending request to %s with username %s payload %s" % (url, args.destUsername, payload))
        res = requests.get(url, auth=(args.destUsername,args.destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok):
            logger.error("URL %s status code %s reason %s, response '%s', in app %s" % (url, res.status_code, res.reason, res.text, args.destApp))
            break
        localRes = json.loads(res.text)
        if localRes["entry"][0]["content"].has_key("alert_condition"):
            logging.debug("%s of sharing level %s with owner %s appears to be an alert" % (name, sharing, owner))
            isAlert = True
    
    actionTaken = False
    if args.enableObj:
        logging.debug("Firing off enable request")
        
        #If this is an alert we set the disabled flag to 0 if enabling
        if isAlert:
            payload["disabled"] = "0"
        else:
            #If it's a savedsearch we reenable the schedule
            payload["is_scheduled"] = "1"
        actionTaken = "enabled"
    elif args.disableObj:
        logging.debug("Firing off disable request")
        #If this is an alert we set the disabled flag to 0 if enabling
        if isAlert:
            payload["disabled"] = "1"
        else:
            #If it's a savedsearch we reenable the schedule
            payload["is_scheduled"] = "0"
        
        actionTaken = "disabled"
    
    if actionTaken:
        logging.debug("Sending request to %s with username %s payload %s" % (url, args.destUsername, payload))
        res = requests.post(url, auth=(args.destUsername,args.destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok):
            logger.error("URL %s status code %s reason %s, response '%s', in app %s" % (url, res.status_code, res.reason, res.text, args.destApp))
            break
        
        logging.info("name %s sharing %s owner %s updated %s in app %s is now %s" % (name, sharing, owner, lastUpdated, args.destApp, actionTaken))
    else:
        logging.info("No enable/disable flags used, name %s sharing %s owner %s updated %s is in scope in app %s" % (name, sharing, owner, lastUpdated, args.destApp))

logging.info("Done")