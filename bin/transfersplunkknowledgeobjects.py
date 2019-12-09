import requests
import xml.etree.ElementTree as ET
import logging
from logging.config import dictConfig
import six.moves.urllib.request, six.moves.urllib.parse, six.moves.urllib.error
import argparse
import json
import copy
from datetime import datetime,timedelta
import re

###########################
#
# transfersplunknowledgeobjects
#   Run this script via splunk cmd python transfersplunknowledgeobjects.py for help information use the -h flag:
#   splunk cmd python transfersplunknowledgeobjects.py -h
#   This script aims to transfer Splunk knowledge objects from 1 instance via REST API queries to another instance via REST API POST's
#   this method allows avoiding the deployer in all cases except for lookups, lookups can be pushed from a deployer and then removed from the deployer
#   before attempting to migrate lookup definitions, this will ensure that the lookup files actually exist when the definitions are created
#
#   Limitations
#       This script re-owns a knowledge object to the owner on the original system unless the -destOwner parameter is specified, note that permissions per-knowledge object *are not*
#       applied and therefore each knowledge object inherits the permissions of the parent application
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
              'filename' : '/tmp/transfer_knowledgeobj.log',
              'formatter': 'f',
              'maxBytes' :  10485760,
              'level': logging.DEBUG,
              'backupCount': 5 }
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
parser.add_argument('-srcURL', help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example', required=True)
parser.add_argument('-destURL', help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example', required=True)
parser.add_argument('-srcUsername', help='username to use for REST API of srcURL argument', required=True)
parser.add_argument('-srcPassword', help='password to use for REST API of srcURL argument', required=True)
parser.add_argument('-srcApp', help='application name on srcURL to be migrated from', required=True)
parser.add_argument('-destApp', help='(optional) application name to be used on destURL to be migrated to defaults to srcApp')
parser.add_argument('-destUsername', help='(optional) username to use for REST API of destURL argument defaults to srcUsername')
parser.add_argument('-destPassword', help='(optional) password to use for REST API of destURL argument defaults to srcPassword')
parser.add_argument('-destOwner', help='(optional) override the username on the destination app when creating the objects')
parser.add_argument('-noPrivate', help='(optional) disable the migration of user level / private objects', action='store_true')
parser.add_argument('-noDisabled', help='(optional) disable the migratio of objects with a disabled status in Splunk', action='store_true')
parser.add_argument('-all', help='(optional) migrate all knowledge objects', action='store_true')
parser.add_argument('-macros', help='(optional) migrate macro knowledge objects', action='store_true')
parser.add_argument('-tags', help='(optional) migrate tag knowledge objects', action='store_true')
parser.add_argument('-eventtypes', help='(optional) migrate event types knowledge objects', action='store_true')
parser.add_argument('-allFieldRelated', help='(optional) migrate all objects under fields', action='store_true')
parser.add_argument('-calcFields', help='(optional) migrate calc fields knowledge objects', action='store_true')
parser.add_argument('-fieldAlias', help='(optional) migrate field alias knowledge objects', action='store_true')
parser.add_argument('-fieldExtraction', help='(optional) migrate field extraction knowledge objects', action='store_true')
parser.add_argument('-fieldTransforms', help='(optional) migrate field transformation knowledge objects (excludes nullQueue formatted transforms)', action='store_true')
parser.add_argument('-lookupDefinition', help='(optional) migrate lookup definition knowledge objects', action='store_true')
parser.add_argument('-workflowActions', help='(optional) migrate workflow actions', action='store_true')
parser.add_argument('-sourcetypeRenaming', help='(optional) migrate sourcetype renaming', action='store_true')
parser.add_argument('-automaticLookup', help='(optional) migrate automation lookup knowledge objects', action='store_true')
parser.add_argument('-datamodels', help='(optional) migrate data model knowledge objects', action='store_true')
parser.add_argument('-dashboards', help='(optional) migrate dashboards (user interface -> views)', action='store_true')
parser.add_argument('-savedsearches', help='(optional) migrate saved search objects (this includes reports/alerts)', action='store_true')
parser.add_argument('-navMenu', help='(optional) migrate navigation menus', action='store_true')
parser.add_argument('-overrideMode', help='(optional) if the remote knowledge object exists, overwrite it with the migrated version only if the migrated version does not have a newer updated time. Skip otherwise', action='store_true')
parser.add_argument('-overrideAlwaysMode', help='(optional) if the remote knowledge object exists, overwrite it with the migrated version (by default this will not override)', action='store_true')
parser.add_argument('-collections', help='(optional) migrate collections (kvstore collections)', action='store_true')
parser.add_argument('-times', help='(optional) migrate time labels (conf-times)', action='store_true')
parser.add_argument('-panels', help='(optional) migrate pre-built dashboard panels', action='store_true')
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-printPasswords', help='(optional) print passwords in the log files (dev only)', action='store_true')
parser.add_argument('-includeEntities', help='(optional) comma separated list of object values to include (double quoted)')
parser.add_argument('-excludeEntities', help='(optional) comma separated list of object values to exclude (double quoted)')
parser.add_argument('-includeOwner', help='(optional) comma separated list of owners objects that should be transferred (double quoted)')
parser.add_argument('-excludeOwner', help='(optional) comma separated list of owners objects that should be transferred (double quoted)')
parser.add_argument('-privateOnly', help='(optional) Only transfer private objects')
parser.add_argument('-viewstates', help='(optional) migrate viewstates', action='store_true')
parser.add_argument('-ignoreViewstatesAttribute', help='(optional) when creating saved searches strip the vsid parameter/attribute before attempting to create the saved search', action='store_true')
parser.add_argument('-disableAlertsOrReportsOnMigration', help='(optional) when creating alerts/reports, set disabled=1 (or enableSched=0) irrelevant of the previous setting pre-migration', action='store_true')
parser.add_argument('-nameFilter', help='(optional) use a regex filter to find the names of the objects to transfer')
parser.add_argument('-sharingFilter', help='(optional) only transfer objects with this level of sharing (user, app, global)', choices=['user', 'app', 'global'])

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

srcApp = args.srcApp
destApp = ""
if args.destApp:
    destApp = args.destApp
else:
    destApp = srcApp

srcUsername = args.srcUsername
destUsername = ""
if args.destUsername:
    destUsername = args.destUsername
else:
    destUsername = srcUsername

srcPassword = args.srcPassword
destPassword = ""
if args.destPassword:
    destPassword = args.destPassword
else:
    destPassword = srcPassword

destOwner=False
if args.destOwner:
    destOwner = args.destOwner

if args.nameFilter:
    nameFilter = re.compile(args.nameFilter)

#From server
splunk_rest = args.srcURL

#Destination server
splunk_rest_dest = args.destURL

#As per https://stackoverflow.com/questions/1101508/how-to-parse-dates-with-0400-timezone-string-in-python/23122493#23122493
def determineTime(timestampStr, name, app, type):
    logger.debug("Attempting to convert %s to timestamp for %s name, in app %s for type %s" % (timestampStr, name, app, type))
    ret = datetime.strptime(timestampStr[0:19],  "%Y-%m-%dT%H:%M:%S")
    if timestampStr[19]=='+':
        ret-=timedelta(hours=int(timestampStr[20:22]),minutes=int(timestampStr[24:]))
    elif timestampStr[19]=='-':
        ret+=timedelta(hours=int(timestampStr[20:22]),minutes=int(timestampStr[24:]))
    logger.debug("Converted time is %s to timestamp for %s name, in app %s for type %s" % (ret, name, app, type))
    return ret

def appendToResults(resultsDict, name, result):
    if not name in resultsDict:
        resultsDict[name] = []
    resultsDict[name].append(result)

def popLastResult(resultsDict, name):
    if name in resultsDict:
        if len(resultsDict[name]) > 0:
            resultsDict[name].pop()

###########################
#
# runQueries (generic version)
#   This attempts to call the REST API of the srcServer, parses the resulting XML data and re-posts the relevant sections to the
#   destination server
#   This method works for everything excluding macros which have a different process
#   Due to variations in the REST API there are a few hacks inside this method to handle specific use cases, however the majority are straightforward
#
###########################
def runQueries(app, endpoint, type, fieldIgnoreList, destApp, aliasAttributes={}, valueAliases={}, nameOverride="", destOwner=False, noPrivate=False, noDisabled=False, override=None, includeEntities=None, excludeEntities=None, includeOwner=None, excludeOwner=None, privateOnly=None, disableAlertsOrReportsOnMigration=False, overrideAlways=None, actionResults=None):
    #Keep a success/Failure list to be returned by this function
    #actionResults = {}

    #Use count=-1 to ensure we see all the objects
    url = splunk_rest + "/servicesNS/-/" + app + endpoint + "?count=-1"
    logger.debug("Running requests.get() on %s with username %s in app %s" % (url, srcUsername, app))

    #Verify=false is hardcoded to workaround local SSL issues
    res = requests.get(url, auth=(srcUsername, srcPassword), verify=False)
    if (res.status_code != requests.codes.ok):
        logger.error("URL %s in app %s status code %s reason %s, response: '%s'" % (url, app, res.status_code, res.reason, res.text))

    #Splunk returns data in XML format, use the element tree to work through it
    root = ET.fromstring(res.text)

    infoList = {}
    for child in root:
        #Working per entry in the results
        if child.tag.endswith("entry"):
            #Down to each entry level
            info = {}
            #Some lines of data we do not want to keep, assume we want it
            keep = True
            for innerChild in child:
                #title / name attribute
                if innerChild.tag.endswith("title"):
                    title = innerChild.text
                    info["name"] = title
                    logger.debug("%s is the name/title of this entry for type %s in app %s" % (title, type, app))
                    #If we have an include/exclude list we deal with that scenario now
                    if includeEntities:
                        if not title in includeEntities:
                            logger.debug("%s of type %s not in includeEntities list in app %s" % (title, type, app))
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug("%s of type %s in excludeEntities list in app %s" % (title, type, app))
                            keep = False
                            break

                    if args.nameFilter:
                        if not nameFilter.search(title):
                            logger.debug("%s of type %s does not match regex in app %s" % (title, type, app))
                            keep = False
                            break

                    #Backup the original name if we override it, override works fine for creation
                    #but updates require the original name
                    if 'name' in list(aliasAttributes.values()):
                        info["origName"] = title

                elif innerChild.tag.endswith("updated"):
                    updatedStr = innerChild.text
                    updated = determineTime(updatedStr, info["name"], app, type)
                    info['updated'] = updated
                    logger.debug("name %s, type %s in app %s was updated on %s" % (title, type, app, updated))
                #Content apepars to be where 90% of the data we want is located
                elif innerChild.tag.endswith("content"):
                    for theAttribute in innerChild[0]:
                        #acl has the owner, sharing and app level which we required (sometimes there is eai:app but it's not 100% consistent so this is safer
                        #also have switched from author to owner as it's likely the safer option...
                        if theAttribute.attrib['name'] == 'eai:acl':
                            for theList in theAttribute[0]:
                                if theList.attrib['name'] == 'sharing':
                                    logger.debug("%s of type %s has sharing %s in app %s" % (info["name"], type, theList.text, app))
                                    info["sharing"] = theList.text
                                    if noPrivate and info["sharing"] == "user":
                                        logger.debug("%s of type %s found but the noPrivate flag is true, excluding this in app %s" % (info["name"], type, app))
                                        keep = False
                                        break
                                    elif privateOnly and info["sharing"] != "user":
                                        logger.debug("%s of type %s found but the privateOnly flag is true and value of %s is not user level sharing (private), excluding this in app %s" % (info["name"], type, info["sharing"], app))
                                        keep = False
                                        break

                                    if args.sharingFilter and not args.sharingFilter == info["sharing"]:
                                        logger.debug("%s of type %s found but the sharing level is set to %s and this object has sharing %s excluding this in app %s" % (info["name"], type, args.sharingFilter, info["sharing"], app))
                                        keep = False
                                        break

                                elif theList.attrib['name'] == 'app':
                                    foundApp = theList.text
                                    logger.debug("%s of type %s in app context of %s belongs to %s" % (info["name"], type, app, foundApp))
                                    #We can see globally shared objects in our app context, it does not mean we should migrate them as it's not ours...
                                    if app != foundApp:
                                        logger.debug("%s of type %s found in app context of %s belongs to app context %s, excluding from app %s" % (info["name"], type, app, foundApp, app))
                                        keep = False
                                        break
                                #owner is seen as a nicer alternative to the author variable
                                elif theList.attrib['name'] == 'owner':
                                    owner = theList.text

                                    #If we have include or exlcude owner lists we deal with this now
                                    if includeOwner:
                                        if not owner in includeOwner:
                                            logger.debug("%s of type %s with owner %s not in includeOwner list in app %s" % (info["name"], type, owner, app))
                                            keep = False
                                            break
                                    if excludeOwner:
                                        if owner in excludeOwner:
                                            logger.debug("%s of type %s with owner %s in excludeOwner list in app %s" % (info["name"], type, owner, app))
                                            keep = False
                                            break
                                    logger.debug("%s of type %s has owner %s in app %s" % (info["name"], type, owner, app))
                                    info["owner"] = owner

                        else:
                            #We have other attributes under content, we want the majority of them
                            attribName = theAttribute.attrib['name']

                            #Under some circumstances we want the attribute and contents but we want to call it a different name...
                            if attribName in aliasAttributes:
                                attribName = aliasAttributes[attribName]

                            #If it's disabled *and* we don't want disabled objects we can determine this here
                            if attribName == "disabled" and noDisabled and theAttribute.text == "1":
                                logger.debug("%s of type %s is disabled and the noDisabled flag is true, excluding this in app %s" % (info["name"], type, app))
                                keep = False
                                break

                            #Field extractions change from "Uses transform" to "REPORT" And "Inline" to "EXTRACTION" for some strange reason...
                            #therefore we have a list of attribute values that we deal with here which get renamed to the provided values
                            if theAttribute.text in valueAliases:
                                theAttribute.text = valueAliases[theAttribute.text]

                            logger.debug("%s of type %s found key/value of %s=%s in app context %s" % (info["name"], type, attribName, theAttribute.text, app))

                            #Yet another hack, this time to deal with collections using accelrated_fields.<value> when looking at it via REST GET requests, but
                            #requiring accelerated_fields to be used when POST'ing the value to create the collection!
                            if type == "collections (kvstore definition)" and attribName.find("accelrated_fields") == 0:
                                attribName = "accelerated_fields" + attribName[17:]

                            #Hack to deal with datamodel tables not working as expected
                            if attribName == "description" and type=="datamodels" and "dataset.type" in info and info["dataset.type"] == "table":
                                #For an unknown reason the table datatype has extra fields in the description which must be removed
                                #however we have to find them first...
                                #For an unknown reason the table datatype has extra fields in the description which must be removed
                                #however we have to find them first...
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
                            #A hack related to automatic lookups, where a None / empty value must be sent through as "", otherwise requests will strip the entry from the
                            #post request. In the case of an automatic lookup we want to send through the empty value...
                            elif type=="automatic lookup" and theAttribute.text == None:
                                info[attribName] = ""
            #If we have not set the keep flag to False
            if keep:
                if nameOverride != "":
                    info["origName"] = info["name"]
                    info["name"] = info[nameOverride]
                    #TODO hack to handle field extractions where they have an extra piece of info in the name
                    #as in the name is prepended with EXTRACT-, REPORT- or LOOKUP-, we need to remove this before creating
                    #the new version
                    if type=="fieldextractions" and info["name"].find("EXTRACT-") == 0:
                        logger.debug("Overriding name of %s of type %s in app context %s with owner %s to new name of %s" % (info["name"], type, app, info["owner"], info["name"][8:]))
                        info["name"] = info["name"][8:]
                    elif type=="fieldextractions" and info["name"].find("REPORT-") == 0:
                        logger.debug("Overriding name of %s of type %s in app context %s with owner %s to new name of %s" % (info["name"], type, app, info["owner"], info["name"][7:]))
                        info["name"] = info["name"][7:]
                    elif type=="automatic lookup" and info["name"].find("LOOKUP-") == 0:
                        logger.debug("Overriding name of %s of type %s in app context %s with owner %s to new name of %s" % (info["name"], type, app, info["owner"], info["name"][7:]))
                        info["name"] = info["name"][7:]
                    elif type=="fieldaliases":
                        newName = info["name"]
                        newName = newName[newName.find("FIELDALIAS-")+11:]
                        logger.debug("Overriding name of %s of type %s in app context %s with owner %s to new name of %s" % (info["name"], type, app, info["owner"], newName))
                        info["name"] = newName
                #Some attributes are not used to create a new version so we remove them...(they may have been used above first so we kept them until now)
                for attribName in fieldIgnoreList:
                    if attribName in info:
                        del info[attribName]

                #Add this to the infoList
                sharing = info["sharing"]
                if sharing not in infoList:
                    infoList[sharing] = []

                #REST API does not support the creation of null queue entries as tested in 7.0.5 and 7.2.1, these are also unused on search heads anyway so ignoring these with a warning
                if type == "fieldtransformations" and "FORMAT" in info and info["FORMAT"] == "nullQueue":
                    logger.warn("Dropping the transfer of %s of type %s in app context %s with owner %s because nullQueue entries cannot be created via REST API (and they are not required in search heads)" % (info["name"], type, app, info["owner"]))
                else:
                    infoList[sharing].append(info)
                    logger.info("Recording %s info for %s in app context %s with owner %s" % (type, info["name"], app, info["owner"]))

                #If we are migrating but leaving the old app enabled in a previous environment we may not want to leave the report and/or alert enabled
                if disableAlertsOrReportsOnMigration and type == "savedsearches":
                    if "disabled" in info and info["disabled"] == "0" and "alert_condition" in info:
                        logger.info("%s of type %s (alert) in app %s with owner %s was enabled but disableAlertsOrReportOnMigration set, setting to disabled" % (type, info["name"], app, info["owner"]))
                        info["disabled"] = 1
                    elif "is_scheduled" in info and "alert_condition" not in info and info["is_scheduled"] == "1":
                        info["is_scheduled"] = 0
                        logger.info("%s of type %s (scheduled report) in app %s with owner %s was enabled but disableAlertsOrReportOnMigration set, setting to disabled" % (type, info["name"], app, info["owner"]))
    app = destApp

    #Cycle through each one we need to migrate, we do global/app/user as users can duplicate app level objects with the same names
    #but we create everything at user level first then re-own it so global/app must happen first
    if "global" in infoList:
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with global level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["global"], destOwner, type, override, app, splunk_rest_dest, endpoint, actionResults, overrideAlways)

    if "app" in infoList:
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with app level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["app"], destOwner, type, override, app, splunk_rest_dest, endpoint, actionResults, overrideAlways)

    if "user" in infoList:
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with user (private) level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["user"], destOwner, type, override, app, splunk_rest_dest, endpoint, actionResults, overrideAlways)

    return actionResults

###########################
#
# runQueriesPerList
#   Runs the required queries to create the knowledge object and then re-owns them to the correct user
#
###########################
def runQueriesPerList(infoList, destOwner, type, override, app, splunk_rest_dest, endpoint, actionResults, overrideAlways):
    for anInfo in infoList:
        sharing = anInfo["sharing"]
        owner = anInfo["owner"]
        if destOwner:
            owner = destOwner

        #We cannot post the sharing/owner information to the REST API, we use them later
        del anInfo["sharing"]
        del anInfo["owner"]

        payload = anInfo
        name = anInfo["name"]
        curUpdated = anInfo["updated"]
        del anInfo["updated"]

        url = "%s/servicesNS/%s/%s/%s" % (splunk_rest_dest, owner, app, endpoint)
        objURL = None
        origName = None
        encoded_name = six.moves.urllib.parse.quote(name.encode('utf-8'))
        encoded_name = encoded_name.replace("/", "%2F")        
        if 'origName' in anInfo:
            origName = anInfo['origName']
            del anInfo['origName']
            logger.debug("%s of type %s overriding name from %s to %s due to origName existing in config dictionary" % (name, type, name, origName))
            objURL = "%s/servicesNS/-/%s/%s/%s?output_mode=json" % (splunk_rest_dest, app, endpoint, origName)
        else:
            #datamodels do not allow /-/ (or private / user level sharing, only app level)
            if type == "datamodels":
                objURL = "%s/servicesNS/%s/%s/%s/%s?output_mode=json" % (splunk_rest_dest, owner, app, endpoint, encoded_name)
            else:
                objURL = "%s/servicesNS/-/%s/%s/%s?output_mode=json" % (splunk_rest_dest, app, endpoint, encoded_name)
        logger.debug("%s of type %s checking on URL %s to see if it exists" % (name, type, objURL))
        #Verify=false is hardcoded to workaround local SSL issues
        res = requests.get(objURL, auth=(destUsername,destPassword), verify=False)
        objExists = False
        updated = None
        createdInAppContext = False

        #If we get 404 it definitely does not exist
        if (res.status_code == 404):
            logger.debug("URL %s is throwing a 404, assuming new object creation" % (objURL))
        elif (res.status_code != requests.codes.ok):
            logger.error("URL %s in app %s status code %s reason %s, response: '%s'" % (objURL, app, res.status_code, res.reason, res.text))
        else:
            #However the fact that we did not get a 404 does not mean it exists in the context we expect it to, perhaps it's global and from another app context?
            #or perhaps it's app level but we're restoring a private object...
            logger.debug("Attempting to JSON loads on %s" % (res.text))
            resDict = json.loads(res.text)
            for entry in resDict['entry']:
                sharingLevel = entry['acl']['sharing']
                appContext = entry['acl']['app']
                updatedStr = entry['updated']
                remoteObjOwner = entry['acl']['owner']
                updated = determineTime(updatedStr, name, app, type)
                if appContext == app and (sharing == 'app' or sharing=='global') and (sharingLevel == 'app' or sharingLevel == 'global'):
                    objExists = True
                    logger.debug("name %s of type %s in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, type, app, objURL, sharingLevel, updated))
                elif appContext == app and sharing == 'user' and sharingLevel == "user" and remoteObjOwner == owner:
                    objExists = True
                    logger.debug("name %s of type %s in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, type, app, objURL, sharingLevel, updated))
                elif appContext == app and sharingLevel == "user" and remoteObjOwner == owner and (sharing == "app" or sharing == "global"):
                    logger.debug("name %s of type %s in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, type, app, objURL, sharingLevel, updated))
                else:
                    logger.debug("name %s of type %s in app context %s, found the object with this name in sharingLevel %s and appContext %s, updated time of %s" % (name, type, app, sharingLevel, appContext, updated))
        #Hack to handle the times (conf-times) not including required attributes for creation in existing entries
        #not sure how this happens but it fails to create in 7.0.5 but works fine in 7.2.x, fixing for the older versions
        if type=="times (conf-times)" and "is_sub_menu" not in payload:
            payload["is_sub_menu"] = "0"

        if sharing == 'app' or sharing=='global':
            url = "%s/servicesNS/nobody/%s/%s" % (splunk_rest_dest, app, endpoint)
            logger.info("name %s of type %s in app context %s, sharing level is non-user so creating with nobody context updated url is %s" % (name, type, app, url))
            createdInAppContext = True

        deletionURL = None
        if objExists == False:
            logger.debug("Attempting to create %s with name %s on URL %s with payload '%s' in app %s" % (type, name, url, payload, app))
            res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
            if (res.status_code != requests.codes.ok and res.status_code != 201):
                logger.error("%s of type %s with URL %s status code %s reason %s, response '%s', in app %s, owner %s" % (name, type, url, res.status_code, res.reason, res.text, app, owner))
                appendToResults(actionResults, 'creationFailure', name)
                continue
            else:
                logger.debug("%s of type %s in app %s with URL %s result is: '%s' owner of %s" % (name, type, app, url, res.text, owner))

            #Parse the result to find the new URL to use
            root = ET.fromstring(res.text)
            infoList = []

            creationSuccessRes = False
            for child in root:
                #Working per entry in the results
                if child.tag.endswith("entry"):
                    #Down to each entry level
                    for innerChild in child:
                        #print innerChild.tag
                        if innerChild.tag.endswith("link") and innerChild.attrib["rel"]=="list":
                            deletionURL = "%s/%s" % (splunk_rest_dest, innerChild.attrib["href"])
                            logger.debug("%s of type %s in app %s recording deletion URL as %s" % (name, type, app, deletionURL))
                            appendToResults(actionResults, 'creationSuccess', deletionURL)
                            creationSuccessRes = True
                elif child.tag.endswith("messages"):
                    for innerChild in child:
                        if innerChild.tag.endswith("msg") and innerChild.attrib["type"]=="ERROR" or "WARN" in innerChild.attrib:
                            logger.warn("%s of type %s in app %s had a warn/error message of '%s' owner of %s" % (name, type, app, innerChild.text, owner))
                            #Sometimes the object appears to be create but is unusable which is annoying, at least provide the warning to the logs
                            #and record it in the failure list for investigation
                            appendToResults(actionResults, 'creationFailure', name)
            if not deletionURL:
                logger.warn("%s of type %s in app %s did not appear to create correctly, will not attempt to change the ACL of this item" % (name, type, app))
                continue
            #Re-owning it to the previous owner
            url = "%s/acl" % (deletionURL)
            payload = { "owner": owner, "sharing" : sharing }
            logger.info("Attempting to change ownership of %s with name %s via URL %s to owner %s in app %s with sharing %s" % (type, name, url, owner, app, sharing))
            res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)

            #If re-own fails consider this a failure that requires investigation
            if (res.status_code != requests.codes.ok):
                logger.error("%s of type %s in app %s with URL %s status code %s reason %s, response '%s', owner of %s" % (name, type, app, url, res.status_code, res.reason, res.text, owner))
                appendToResults(actionResults, 'creationFailure', name)
                if res.status_code == 409:
                    if type == "eventtypes":
                        logger.warn("Received a 409 while changing the ACL permissions of %s of type %s in app %s with URL %s, however eventtypes throw this error and work anyway. Ignoring!" % (name, type, app, url))
                        continue
                    #Delete the duplicate private object rather than leave it there for no reason
                    url = url[:-4]
                    logger.warn("Deleting the private object as it could not be re-owned %s of type %s in app %s with URL %s" % (name, type, app, url))
                    requests.delete(url, auth=(destUsername,destPassword), verify=False)
                    #If we previously recorded success, remove that entry
                    if creationSuccessRes:
                        popLastResult(actionResults, 'creationSuccess')
                continue
            else:
                logger.debug("%s of type %s in app %s, ownership changed with response: %s, will update deletion URL, owner %s, sharing level %s" % (name, type, app, res.text, owner, sharing))

                #Parse the return response
                root = ET.fromstring(res.text)
                infoList = []
                for child in root:
                    #Working per entry in the results
                    if child.tag.endswith("entry"):
                        #Down to each entry level
                        for innerChild in child:
                            if innerChild.tag.endswith("link") and innerChild.attrib["rel"]=="list":
                                deletionURL = "%s/%s" % (splunk_rest_dest, innerChild.attrib["href"])
                                logger.debug("%s of type %s in app %s recording new deletion URL as %s , owner is %s and sharing level %s" % (name, type, app, deletionURL, owner, sharing))
                                #Remove our last recorded URL
                                #logger.debug("action results is %s" % (actionResults))
                                popLastResult(actionResults, 'creationSuccess')
                                appendToResults(actionResults, 'creationSuccess', deletionURL)
                                #logger.debug("action results is %s" % (actionResults))
            if creationSuccessRes:
                logger.info("Created %s of type %s in app %s owner is %s sharing level %s" % (name, type, app, owner, sharing))
            else:
                logger.warn("Atempted to create %s of type %s in app %s owner is %s sharing level %s but failed" % (name, type, app, owner, sharing))
        else:
            #object exists already
            if override or overrideAlways:
                #If the override flag is on and the curUpdated attribute is older than the remote updated attribute
                #then do not continue with the override
                #if we have overrideAlways we blindly overwrite
                if override and curUpdated < updated:
                    logger.info("%s of type %s in app %s with URL %s, owner %s, source object says time of %s, destination object says time of %s, skipping this entry" % (name, type, app, objURL, owner, curUpdated, updated))
                    appendToResults(actionResults, 'creationSkip', objURL)
                    continue
                else:
                    logger.info("%s of type %s in app %s with URL %s, owner %s, source object says time of %s, destination object says time of %s, will update this entry" % (name, type, app, objURL, owner, curUpdated, updated))
                url = objURL

                #If we're user level we want to update using the user endpoint
                #If it's app/global we update using the nobody to ensure we're updating the app level object
                urlName = None
                if origName:
                    urlName = origName
                else:
                    urlName = encoded_name

                if sharing == "user":
                    url = "%s/servicesNS/%s/%s/%s/%s" % (splunk_rest_dest, owner, app, endpoint, urlName)
                else:
                    url = "%s/servicesNS/nobody/%s/%s/%s" % (splunk_rest_dest, app, endpoint, urlName)

                #Cannot post type/stanza when updating field extractions or a few other object types, but require them for creation?!
                if 'type' in payload:
                    del payload['type']
                if 'stanza' in payload:
                    del payload['stanza']

                #Remove the name from the payload
                del payload['name']
                logger.debug("Attempting to update %s with name %s on URL %s with payload '%s' in app %s" % (type, name, url, payload, app))
                res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
                if (res.status_code != requests.codes.ok and res.status_code != 201):
                    logger.error("%s of type %s with URL %s status code %s reason %s, response '%s', in app %s, owner %s" % (name, type, url, res.status_code, res.reason, res.text, app, owner))
                    appendToResults(actionResults, 'updateFailure', name)
                else:
                    logger.debug("Post-update of %s of type %s in app %s with URL %s result is: '%s' owner of %s" % (name, type, app, url, res.text, owner))
                    appendToResults(actionResults, 'updateSuccess', name)

                #Re-owning it to the previous owner
                if sharing != "user":
                    url = "%s/acl" % (url)
                    payload = { "owner": owner, "sharing" : sharing }
                    logger.info("App or Global sharing in use, attempting to change ownership of %s with name %s via URL %s to owner %s in app %s with sharing %s" % (type, name, url, owner, app, sharing))
                    res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
            else:
                appendToResults(actionResults, 'creationSkip', objURL)
                logger.info("%s of type %s in app %s owner of %s, object already exists and override is not set, nothing to do here" % (name, type, app, owner))

###########################
#
# macros
#
###########################
#macro use cases are slightly different to everything else on the REST API
#enough that this code has not been integrated into the runQuery() function
def macros(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, macroResults):
    macros = {}
    #servicesNS/-/-/properties/macros doesn't show private macros so using /configs/conf-macros to find all the macros
    #again with count=-1 to find all the available macros
    url = splunk_rest + "/servicesNS/-/" + app + "/configs/conf-macros?count=-1"
    logger.debug("Running requests.get() on %s with username %s in app %s for type macro" % (url, srcUsername, app))
    res = requests.get(url, auth=(srcUsername, srcPassword), verify=False)
    if (res.status_code != requests.codes.ok):
        logger.error("Type macro in app %s, URL %s status code %s reason %s, response '%s'" % (app, url, res.status_code, res.reason, res.text))

    #Parse the XML tree
    root = ET.fromstring(res.text)

    for child in root:
        #Working per entry in the results
        if child.tag.endswith("entry"):
            #Down to each entry level
            macroInfo = {}
            keep = True
            for innerChild in child:
                #title is the name
                if innerChild.tag.endswith("title"):
                    title = innerChild.text
                    macroInfo["name"] = title
                    logger.debug("Found macro title/name: %s in app %s" % (title, app))
                    #Deal with the include/exclude lists
                    if includeEntities:
                        if not title in includeEntities:
                            logger.debug("%s of type macro not in includeEntities list in app %s" % (title, app))
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug("%s of type macro in excludeEntities list in app %s" % (title, app))
                            keep = False
                            break

                    if args.nameFilter:
                        if not nameFilter.search(title):
                            logger.debug("%s of type macro does not match regex in app %s" % (title, app))
                            keep = False
                            break

                elif innerChild.tag.endswith("updated"):
                    updatedStr = innerChild.text
                    updated = determineTime(updatedStr, macroInfo["name"], app, "macro")
                    macroInfo['updated'] = updated
                    logger.debug("name %s, of type macro in app %s was updated on %s" % (title, app, updated))
                #Content apepars to be where 90% of the data we want is located
                elif innerChild.tag.endswith("content"):
                    for theAttribute in innerChild[0]:
                        #acl has the owner, sharing and app level which we required (sometimes there is eai:app but it's not 100% consistent so this is safer
                        #also have switched from author to owner as it's likely the safer option...
                        if theAttribute.attrib['name'] == 'eai:acl':
                            for theList in theAttribute[0]:
                                if theList.attrib['name'] == 'sharing':
                                    logger.debug("%s of type macro sharing: %s in app %s" % (macroInfo["name"], theList.text, app))
                                    macroInfo["sharing"] = theList.text

                                    #If we are excluding private then check the sharing is not user level (private in the GUI)
                                    if noPrivate and macroInfo["sharing"] == "user":
                                        logger.debug("%s of type macro found but the noPrivate flag is true, excluding this in app %s" % (macroInfo["name"], app))
                                        keep = False
                                        break
                                    elif privateOnly and macroInfo["sharing"] != "user":
                                        logger.debug("%s of type macro found but the privateOnly flag is true and sharing is %s, excluding this in app %s" % (macroInfo["name"], macroInfo["sharing"], app))
                                        keep = False
                                        break

                                    if args.sharingFilter and not args.sharingFilter == info["sharing"]:
                                        logger.debug("%s of type macro found but the sharing level is set to %s and this object has sharing %s excluding this in app %s" % (macroInfo["name"], args.sharingFilter, macroInfo["sharing"], app))
                                        keep = False
                                        break

                                elif theList.attrib['name'] == 'app':
                                    logger.debug("macro app: %s" % (theList.text))
                                    foundApp = theList.text
                                    #We can see globally shared objects in our app context, it does not mean we should migrate them as it's not ours...
                                    if app != foundApp:
                                        logger.debug("%s of type macro found in app context of %s, belongs to app context %s, excluding it" % (macroInfo["name"], app, foundApp))
                                        keep = False
                                        break
                                #owner is used as a nicer alternative to the author
                                elif theList.attrib['name'] == 'owner':
                                    macroInfo["owner"] = theList.text
                                    owner = theList.text
                                    logger.debug("%s of type macro owner is %s" % (macroInfo["name"], owner))
                                    if includeOwner:
                                        if not owner in includeOwner:
                                            logger.debug("%s of type macro with owner %s not in includeOwner list in app %s" % (macroInfo["name"], owner, app))
                                            keep = False
                                            break
                                    if excludeOwner:
                                        if owner in excludeOwner:
                                            logger.debug("%s of type macro with owner %s in excludeOwner list in app %s" % (macroInfo["name"], owner, app))
                                            keep = False
                                            break
                        else:
                            #We have other attributes under content, we want the majority of them
                            attribName = theAttribute.attrib['name']
                            #Check if we have hit hte disabled attribute and we have a noDisabled flag
                            if attribName == "disabled" and noDisabled and theAttribute.text == "1":
                                logger.debug("noDisabled flag is true, %s of type macro is disabled, excluded in app %s" % (theAttribute.attrib['name'], app))
                                keep = False
                                break
                            else:
                                #Otherwise we want this attribute
                                attribName = theAttribute.attrib['name']
                                #Some attributes do not work with the REST API or should not be migrated...
                                logger.debug("%s of type macro key/value pair of %s=%s in app %s" % (macroInfo["name"], attribName, theAttribute.text, app))
                                macroInfo[attribName] = theAttribute.text
            if keep:

                #Add this to the infoList
                sharing = macroInfo["sharing"]
                if sharing not in macros:
                    macros[sharing] = []
                macros[sharing].append(macroInfo)
                logger.info("Recording macro info for %s in app %s with owner %s sharing level of %s" % (macroInfo["name"], app, macroInfo["owner"], macroInfo["sharing"]))

    app = destApp

    #Cycle through each one we need to migrate, we do globa/app/user as users can duplicate app level objects with the same names
    #but we create everything at user level first then re-own it so global/app must happen first
    if "global" in macros:
        logger.debug("Now running macroCreation with knowledge objects of type macro with global level sharing in app %s" % (app))
        macroCreation(macros["global"], destOwner, app, splunk_rest_dest, macroResults, override, overrideAlways)

    if "app" in macros:
        logger.debug("Now running macroCreation with knowledge objects of type macro with app level sharing in app %s" % (app))
        macroCreation(macros["app"], destOwner, app, splunk_rest_dest, macroResults, override, overrideAlways)

    if "user" in macros:
        logger.debug("Now running macroCreation with knowledge objects of type macro with user (private) level sharing in app %s" % (app))
        macroCreation(macros["user"], destOwner, app, splunk_rest_dest, macroResults, override, overrideAlways)

    return macroResults

###########################
#
# macroCreation
#   Runs the required queries to create the macro knowledge objects and then re-owns them to the correct user
#
###########################

def macroCreation(macros, destOwner, app, splunk_rest_dest, macroResults, override, overrideAlways):
    for aMacro in macros:
        sharing = aMacro["sharing"]
        name = aMacro["name"]
        owner = aMacro["owner"]
        curUpdated = aMacro["updated"]
        del aMacro["updated"]

        if destOwner:
            owner = destOwner

        url = "%s/servicesNS/%s/%s/properties/macros" % (splunk_rest_dest, owner, app)

        encoded_name = six.moves.urllib.parse.quote(name.encode('utf-8'))
        encoded_name = encoded_name.replace("/", "%2F")        
        objURL = "%s/servicesNS/-/%s/configs/conf-macros/%s?output_mode=json" % (splunk_rest_dest, app, encoded_name)
        #Verify=false is hardcoded to workaround local SSL issues
        res = requests.get(objURL, auth=(destUsername,destPassword), verify=False)
        logger.debug("%s of type macro checking on URL %s to see if it exists" % (name, objURL))
        objExists = False
        updated = None
        createdInAppContext = False
        #If we get 404 it definitely does not exist
        if (res.status_code == 404):
            logger.debug("URL %s is throwing a 404, assuming new object creation" % (objURL))
        elif (res.status_code != requests.codes.ok):
            logger.error("URL %s in app %s status code %s reason %s, response: '%s'" % (objURL, app, res.status_code, res.reason, res.text))
        else:
            #However the fact that we did not get a 404 does not mean it exists in the context we expect it to, perhaps it's global and from another app context?
            #or perhaps it's app level but we're restoring a private object...
            logger.debug("Attempting to JSON loads on %s" % (res.text))
            resDict = json.loads(res.text)
            for entry in resDict['entry']:
                sharingLevel = entry['acl']['sharing']
                appContext = entry['acl']['app']
                updatedStr = entry['updated']
                updated = determineTime(updatedStr, name, app, "macro")
                remoteObjOwner = entry['acl']['owner']
                logger.info("sharing level %s, app context %s, remoteObjOnwer %s, app %s" % (sharing, appContext, remoteObjOwner, app))
                if appContext == app and (sharing == 'app' or sharing=='global') and (sharingLevel == 'app' or sharingLevel == 'global'):
                    objExists = True
                    logger.debug("name %s of type macro in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, app, objURL, sharingLevel, updated))
                elif appContext == app and sharing == 'user' and sharingLevel == "user" and remoteObjOwner == owner:
                    objExists = True
                    logger.debug("name %s of type macro in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, app, objURL, sharingLevel, updated))
                elif appContext == app and sharingLevel == "user" and remoteObjOwner == owner and (sharing == "app" or sharing == "global"):
                    logger.debug("name %s of type macro in app context %s found to exist on url %s with sharing of %s, updated time of %s" % (name, app, objURL, sharingLevel, updated))
                else:
                    logger.debug("name %s of type macro in app context %s, found the object with this name in sharingLevel %s and appContext %s, updated time of %s, url %s" % (name, app, sharingLevel, appContext, updated, objURL))

        if sharing == 'app' or sharing=='global':
            url = "%s/servicesNS/nobody/%s/properties/macros" % (splunk_rest_dest, app)
            logger.info("name %s of type macro in app context %s, sharing level is non-user so creating with nobody context updated url is %s" % (name, app, url))
            createdInAppContext = True

        if objExists == True and not (override or overrideAlways):
            logger.info("%s of type macro in app %s on URL %s exists, however override/overrideAlways is not set so not changing this macro" % (name, app, objURL))
            appendToResults(macroResults, 'creationSkip', objURL)
            continue
        elif objExists == True and override and not curUpdated > updated:
            logger.info("%s of type macro in app %s on URL %s exists, override is set but the source copy has modification time of %s destination has time of %s, skipping" % (name, app, objURL, curUpdated, updated))
            appendToResults(macroResults, 'creationSkip', objURL)
            continue

        createOrUpdate = None
        if objExists == True:
            createOrUpdate = "update"
            url = url + "/" + encoded_name
        else:
            createOrUpdate = "create"

        logger.info("Attempting to %s macro %s on URL with name %s in app %s" % (createOrUpdate, name, url, app))

        payload = { "__stanza" : name }
        #Create macro
        #I cannot seem to get this working on the /conf URL but this works so good enough, and it's in the REST API manual...
        #servicesNS/-/search/properties/macros
        #__stanza = <name>
        macroCreationSuccessRes = False
        if objExists == False:
            macroCreationSuccessRes = False
            res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
            if (res.status_code != requests.codes.ok and res.status_code != 201):
                logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s', owner %s" % (name, app, url, res.status_code, res.reason, res.text, owner))
                appendToResults(macroResults, 'creationFailure', name)
                if res.status_code == 409:
                    #Delete the duplicate private object rather than leave it there for no purpose
                    url = url[:-4]
                    logger.warn("Deleting the private object as it could not be re-owned %s of type macro in app %s with URL %s" % (name, app, url))
                    requests.delete(url, auth=(destUsername,destPassword), verify=False)
            else:
                #Macros always delete with the username in this URL context
                deletionURL = "%s/servicesNS/%s/%s/configs/conf-macros/%s" % (splunk_rest_dest, owner, app, name)
                logger.debug("%s of type macro in app %s recording deletion URL as %s with owner %s" % (name, app, deletionURL, owner))
                appendToResults(macroResults, 'creationSuccess', deletionURL)
                macroCreationSuccessRes = True

            logger.debug("%s of type macro in app %s, received response of: '%s'" % (name, app, res.text))

        payload = {}

        #Remove parts that cannot be posted to the REST API, sharing/owner we change later
        del aMacro["sharing"]
        del aMacro["name"]
        del aMacro["owner"]
        payload = aMacro

        if createOrUpdate == "create":
            url = url + "/" + encoded_name

        logger.debug("Attempting to modify macro %s on URL %s with payload '%s' in app %s" % (name, url, payload, app))
        res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok and res.status_code != 201):
            logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s'" % (name, app, url, res.status_code, res.reason, res.text))

            if objExists == False:
                appendToResults(macroResults, 'creationFailure', name)
                popLastResult(macroResults, 'macroCreationSuccess')
                logger.warn("Deleting the private object as it could not be modified %s of type macro in app %s with URL %s" % (name, app, url))
                requests.delete(url, auth=(destUsername,destPassword), verify=False)
            else:
                appendToResults(macroResults, 'updateFailure', name)

            macroCreationSuccessRes = False
        else:
            if not createdInAppContext:
                #Re-owning it, I've switched URL's again here but it seems to be working so will not change it
                url = "%s/servicesNS/%s/%s/configs/conf-macros/%s/acl" % (splunk_rest_dest, owner, app, encoded_name)
                payload = { "owner": owner, "sharing" : sharing }
                logger.info("Attempting to change ownership of macro %s via URL %s to owner %s in app %s with sharing %s" % (name, url, owner, app, sharing))
                res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
                if (res.status_code != requests.codes.ok):
                    logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s', owner %s sharing level %s" % (name, app, url, res.status_code, res.reason, res.text, owner, sharing))
                    #Hardcoded deletion URL as if this fails it should be this URL...(not parsing the XML here to confirm but this works fine)
                    deletionURL = "%s/servicesNS/%s/%s/configs/conf-macros/%s" % (splunk_rest_dest, owner, app, name)
                    logger.info("%s of type macro in app %s recording deletion URL as user URL due to change ownership failure %s" % (name, app, deletionURL))
                    #Remove the old record
                    if objExists == False:
                        popLastResult(macroResults, 'macroCreationSuccess')
                        macroCreationSuccessRes = False
                        url = url[:-4]
                        logger.warn("Deleting the private object as it could not be modified %s of type macro in app %s with URL %s" % (name, app, url))
                        requests.delete(url, auth=(destUsername,destPassword), verify=False)
                        appendToResults(macroResults, 'creationFailure', name)
                    else:
                        appendToResults(macroResults, 'updateFailure', name)
                        macroCreationSuccessRes = False
                else:
                    macroCreationSuccessRes = True
                    logger.debug("%s of type macro in app %s, ownership changed with response '%s', new owner %s and sharing level %s" % (name, app, res.text, owner, sharing))
                    if objExists == True:
                        appendToResults(macroResults, 'updateSuccess', name)
            else:
                macroCreationSuccessRes = True
                appendToResults(macroResults, 'updateSuccess', name)

            if macroCreationSuccessRes:
                logger.info("%s %s of type macro in app %s owner is %s sharing level %s was successful" % (createOrUpdate, name, app, owner, sharing))
            else:
               logger.warn("%s %s of type macro in app %s owner is %s sharing level %s was not successful, a failure occurred" % (createOrUpdate, name, app, owner, sharing))

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
    ignoreList = [ "disabled", "eai:appName", "eai:digest", "eai:userName", "isDashboard", "isVisible", "label", "rootNode", "description", "version" ]
    return runQueries(app, "/data/ui/views", "dashboard", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# Saved Searches
#
###########################
def savedsearches(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, ignoreVSID, disableAlertsOrReportsOnMigration, override, overrideAlways, actionResults):
    ignoreList = [ "embed.enabled", "triggered_alert_count" ]

    #View states gracefully fail in the GUI, via the REST API you cannot create the saved search if the view state does not exist
    #however the same saved search can work fine on an existing search head (even though the viewstate has been deleted)
    if ignoreVSID:
        ignoreList.append("vsid")

    return runQueries(app, "/saved/searches", "savedsearches", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, disableAlertsOrReportsOnMigration=disableAlertsOrReportsOnMigration, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# field definitions
#
###########################
def calcfields(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute", "type" ]
    aliasAttributes = { "field.name" : "name" }
    return runQueries(app, "/data/props/calcfields", "calcfields", ignoreList, destApp, aliasAttributes, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def fieldaliases(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute", "type", "value" ]
    return runQueries(app, "/data/props/fieldaliases", "fieldaliases", ignoreList, destApp, nameOverride="name", destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def fieldextractions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute" ]
    return runQueries(app, "/data/props/extractions", "fieldextractions", ignoreList, destApp, {}, { "Inline" : "EXTRACT", "Uses transform" : "REPORT" }, "attribute", destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def fieldtransformations(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute", "DEFAULT_VALUE", "DEPTH_LIMIT", "LOOKAHEAD", "MATCH_LIMIT", "WRITE_META", "eai:appName", "eai:userName", "DEST_KEY" ]
    return runQueries(app, "/data/transforms/extractions", "fieldtransformations", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def workflowactions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/data/ui/workflow-actions", "workflow-actions", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def sourcetyperenaming(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute", "disabled", "eai:appName", "eai:userName", "stanza", "type" ]
    return runQueries(app, "/data/props/sourcetype-rename", "sourcetype-rename", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# tags
#
##########################
def tags(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/configs/conf-tags", "tags", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# eventtypes
#
##########################
def eventtypes(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/saved/eventtypes", "eventtypes", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# navMenus
#
##########################
def navMenu(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "eai:digest", "rootNode" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/data/ui/nav", "navMenu", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, override=override, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# data models
#
##########################
def datamodels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "eai:digest", "eai:type", "acceleration.allowed" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/datamodel/model", "datamodels", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# collections
#
##########################
def collections(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "eai:appName", "eai:userName", "type" ]
    #nobody is the only username that can be used when working with collections
    return runQueries(app, "/storage/collections/config", "collections (kvstore definition)", ignoreList, destApp,destOwner="nobody", noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# viewstates
#
##########################
def viewstates(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "eai:appName", "eai:userName" ]
    #nobody is the only username that can be used when working with collections
    return runQueries(app, "/configs/conf-viewstates", "viewstates", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# time labels (conf-times)
#
##########################
def times(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "header_label" ]
    return runQueries(app, "/configs/conf-times", "times (conf-times)", ignoreList, destApp,destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# panels
#
##########################
def panels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:digest", "panel.title", "rootNode", "eai:appName", "eai:userName" ]
    return runQueries(app, "/data/ui/panels", "pre-built dashboard panels", ignoreList, destApp,destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# lookups (definition/automatic)
#
##########################
def lookupDefinitions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "CAN_OPTIMIZE", "CLEAN_KEYS", "DEPTH_LIMIT", "KEEP_EMPTY_VALS", "LOOKAHEAD", "MATCH_LIMIT", "MV_ADD", "SOURCE_KEY", "WRITE_META", "fields_array", "type" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/data/transforms/lookups", "lookup definition", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

def automaticLookups(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly, override, overrideAlways, actionResults):
    ignoreList = [ "attribute", "type", "value" ]
    return runQueries(app, "/data/props/lookups", "automatic lookup", ignoreList, destApp, {}, {}, "attribute", destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly, override=override, overrideAlways=overrideAlways, actionResults=actionResults)

###########################
#
# Logging functions for the output we provide at the end of a migration run
#
##########################

def logDeletionScriptInLogs(theDict, printPasswords, destUsername, destPassword):
    list = None

    #If we have nothing to log return
    if not theDict:
        return
    if 'creationSuccess' in theDict:
        list = theDict['creationSuccess']
    else:
        return

    for item in list:
        item = item.replace("(", "\(").replace(")","\)")
        if printPasswords:
            logger.info("curl -k -u %s:%s --request DELETE %s" % (destUsername, destPassword, item))
        else:
            logger.info("curl -k --request DELETE %s" % (item))

def logCreationFailure(theDict, type, app):
    list = None
    #If we have nothing to log return
    if not theDict:
        return
    if 'creationFailure' in theDict:
        list = theDict['creationFailure']
    else:
        return

    for item in list:
        logger.warn("In App %s, %s '%s' failed to create" % (app, type, item))

def logStats(resultsDict, type, app):
    successList = []
    failureList = []
    skippedList = []
    updateSuccess = []
    updateFailure = []

    #If we have nothing to log return
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

    logger.info("App %s, %d %s successfully migrated %d %s failed to migrate, %s were skipped due to existing already, %s were updated, %s failed to update" % (app, len(successList), type, len(failureList), type, len(skippedList), len(updateSuccess), len(updateFailure)))

def handleFailureLogging(failureList, type, app):
    logCreationFailure(failureList, type, app)

def logDeletion(successList, printPasswords, destUsername, destPassword):
    logDeletionScriptInLogs(successList, printPasswords, destUsername, destPassword)

#helper function as per https://stackoverflow.com/questions/31433989/return-copy-of-dictionary-excluding-specified-keys
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
dashboardResults= None
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

#If the all switch is provided, migrate everything
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

#All field related switches on anything under Settings -> Fields
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
#   we also do the same trick for owners so we can include only some users or exclude some users from migration
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

excludedList = [ "srcPassword", "destPassword" ]
cleanArgs = without_keys(vars(args), excludedList)
logger.info("transfer splunk knowledge objects run with arguments %s" % (cleanArgs))

src_app_list = None
#Wildcarded app names...use regex
if srcApp.find("*") != -1:
    src_app_list = []
    url = splunk_rest + "/services/apps/local?search=disabled%3D0&f=title&count=0&output_mode=json"
    app_pattern = re.compile(srcApp)
    #Verify=false is hardcoded to workaround local SSL issues
    res = requests.get(url, auth=(srcUsername,srcPassword), verify=False)
    resDict = json.loads(res.text)
    for entry in resDict['entry']:
        logger.debug("entry name is %s, pattern is %s" % (entry['name'], srcApp))
        if app_pattern.search(entry['name']):
            logger.info("Adding app %s to the list of apps" % (entry['name']))
            src_app_list.append(entry['name'])

###########################
#
# Run the required functions based on the args
#   Based on the command line parameters actually run the functions which will migrate the knowledge objects
#
##########################
if args.macros:
    logger.info("Begin macros transfer")
    macroResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            macros(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, macroResults)
    else:
        macros(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, macroResults)
    logger.info("End macros transfer")

if args.tags:
    logger.info("Begin tags transfer")
    tagsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            tags(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, tagsResults)
    else:
        tagsResults = tags(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, tagsResults)
    logger.info("End tags transfer")

if args.eventtypes:
    logger.info("Begin eventtypes transfer")
    eventTypesResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            eventtypes(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, eventTypesResults)
    else:
        eventtypes(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, eventTypesResults)
    logger.info("End eventtypes transfer")

if args.calcFields:
    logger.info("Begin calcFields transfer")
    calcfieldsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            calcfields(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, calcfieldsResults)
    else:
        calcfields(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, calcfieldsResults)
    logger.info("End calcFields transfer")

if args.fieldAlias:
    logger.info("Begin fieldAlias transfer")
    fieldaliasesResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            fieldaliases(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldaliasesResults)
    else:
        fieldaliases(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldaliasesResults)
    logger.info("End fieldAlias transfer")

if args.fieldTransforms:
    logger.info("Begin fieldTransforms transfer")
    fieldTransformationsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            fieldtransformations(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldTransformationsResults)
    else:
        fieldTransformationsResults = fieldtransformations(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldTransformationsResults)
    logger.info("End fieldTransforms transfer")

if args.fieldExtraction:
    logger.info("Begin fieldExtraction transfer")
    fieldextractionsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            fieldextractions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldextractionsResults)
    else:
        fieldextractions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, fieldextractionsResults)
    logger.info("End fieldExtraction transfer")

if args.collections:
    logger.info("Begin collections (kvstore definition) transfer")
    collectionsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            collections(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, collectionsResults)
    else:
        collections(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, collectionsResults)
    logger.info("End collections (kvstore definition) transfer")

if args.lookupDefinition:
    logger.info("Begin lookupDefinitions transfer")
    lookupDefinitionsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            lookupDefinitions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, lookupDefinitionsResults)
    else:
        lookupDefinitions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, lookupDefinitionsResults)
    logger.info("End lookupDefinitions transfer")

if args.automaticLookup:
    logger.info("Begin automaticLookup transfer")
    automaticLookupsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            automaticLookups(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, automaticLookupsResults)
    else:
        automaticLookups(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, automaticLookupsResults)
    logger.info("End automaticLookup transfer")

if args.times:
    logger.info("Begin times (conf-times) transfer")
    timesResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            times(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, timesResults)
    else:
        times(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, timesResults)
    logger.info("End times (conf-times) transfer")

if args.viewstates:
    logger.info("Begin viewstates transfer")
    viewstatesResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            viewstates(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, viewstatesResults)
    else:
        viewstates(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, viewstatesResults)
    logger.info("End viewstates transfer")

if args.panels:
    logger.info("Begin pre-built dashboard panels transfer")
    panelsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            panels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, panelsResults)
    else:
        panels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, panelsResults)
    logger.info("End pre-built dashboard panels transfer")

if args.datamodels:
    logger.info("Begin datamodels transfer")
    datamodelResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            datamodels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, datamodelResults)
    else:
        datamodels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, datamodelResults)
    logger.info("End datamodels transfer")

if args.dashboards:
    logger.info("Begin dashboards transfer")
    dashboardResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            dashboards(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, dashboardResults)
    else:
        dashboards(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, dashboardResults)
    logger.info("End dashboards transfer")

if args.savedsearches:
    logger.info("Begin savedsearches transfer")
    savedSearchResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            savedsearches(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.ignoreViewstatesAttribute, args.disableAlertsOrReportsOnMigration, args.overrideMode, args.overrideAlwaysMode, savedSearchResults)
    else:
        savedsearches(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.ignoreViewstatesAttribute, args.disableAlertsOrReportsOnMigration, args.overrideMode, args.overrideAlwaysMode, savedSearchResults)
    logger.info("End savedsearches transfer")

if args.workflowActions:
    logger.info("Begin workflowActions transfer")
    workflowActionsResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            workflowactions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, workflowActionsResults)
    else:
        workflowactions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, workflowActionsResults)
    logger.info("End workflowActions transfer")

if args.sourcetypeRenaming:
    logger.info("Begin sourcetypeRenaming transfer")
    sourcetypeRenamingResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            sourcetyperenaming(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, sourcetypeRenamingResults)
    else:
        sourcetyperenaming(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, sourcetypeRenamingResults)
    logger.info("End sourcetypeRenaming transfer")

if args.navMenu:
    logger.info("Begin navMenu transfer")
    navMenuResults = {}
    if src_app_list:
        for srcApp in src_app_list:
            navMenu(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, navMenuResults)
    else:
        navMenu(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly, args.overrideMode, args.overrideAlwaysMode, navMenuResults)
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

logger.info("The undo command is: grep -o \"curl.*DELETE.*\" /tmp/transfer_knowledgeobj.log | grep -v \"curl\.\*DELETE\"")
logger.info("Done")
