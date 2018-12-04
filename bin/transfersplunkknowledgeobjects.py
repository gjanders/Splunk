import requests
import xml.etree.ElementTree as ET
import logging
from logging.config import dictConfig
import urllib
import argparse
import json
import copy

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
parser.add_argument('-tag', help='(optional) migrate tag knowledge objects', action='store_true')
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
parser.add_argument('-navMenuWithDefaultOverride', help='(optional) migrate navigation menus *and* overwrite the default menu in the dest app', action='store_true')
parser.add_argument('-collections', help='(optional) migrate collections (kvstore collections)', action='store_true')
parser.add_argument('-times', help='(optional) migrate time labels (conf-times)', action='store_true')
parser.add_argument('-panels', help='(optional) migrate pre-built dashboard panels', action='store_true')
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-printPasswords', help='(optional) print passwords in the log files (dev only)', action='store_true')
parser.add_argument('-includeEntities', help='comma separated list of object values to include (double quoted)')
parser.add_argument('-excludeEntities', help='comma separated list of object values to exclude (double quoted)')
parser.add_argument('-includeOwner', help='comma separated list of owners objects that should be transferred (double quoted)')
parser.add_argument('-excludeOwner', help='comma separated list of owners objects that should be transferred (double quoted)')
parser.add_argument('-privateOnly', help='Only transfer private objects')
parser.add_argument('-viewstates', help='(optional) migrate viewstates', action='store_true')

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

#From server
splunk_rest = args.srcURL

#Destination server
splunk_rest_dest = args.destURL

###########################
#
# runQueries (generic version)
#   This attempts to call the REST API of the srcServer, parses the resulting XML data and re-posts the relevant sections to the 
#   destination server
#   This method works for everything excluding macros which have a different process
#   Due to variations in the REST API there are a few hacks inside this method to handle specific use cases, however the majority are straightforward
# 
###########################
def runQueries(app, endpoint, type, fieldIgnoreList, destApp, aliasAttributes={}, valueAliases={}, nameOverride="", destOwner=False, noPrivate=False, noDisabled=False, override=False, includeEntities=None, excludeEntities=None, includeOwner=None, excludeOwner=None, privateOnly=None):

    #Keep a success/Failure list to be returned by this function
    creationSuccess = []
    creationFailure = []
    
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
                            logger.debug("%s of type %s not in includeEntities list in app %s" % (info["name"], type, app))
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug("%s of type %s in excludeEntities list in app %s" % (info["name"], type, app))
                            keep = False
                            break
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
                                elif theList.attrib['name'] == 'app':
                                    foundApp = theList.text
                                    logger.debug("%s of type %s in app context of %s belongs to %s" % (info["name"], type, app, foundApp))
                                    #We can see globally shared objects in our app context, it does not mean we should migrate them as it's not ours...
                                    if app != foundApp:
                                        logging.debug("%s of type %s found in app context of %s belongs to app context %s, excluding from app %s" % (info["name"], type, app, foundApp, app))
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
                            if aliasAttributes.has_key(attribName):
                                attribName = aliasAttributes[attribName]
                            
                            #If it's disabled *and* we don't want disabled objects we can determine this here
                            if attribName == "disabled" and noDisabled and theAttribute.text == "1":
                                logger.debug("%s of type %s is disabled and the noDisabled flag is true, excluding this in app %s" % (info["name"], type, app))
                                keep = False
                                break
                                
                            #Field extractions change from "Uses transform" to "REPORT" And "Inline" to "EXTRACTION" for some strange reason...
                            #therefore we have a list of attribute values that we deal with here which get renamed to the provided values
                            if valueAliases.has_key(theAttribute.text):
                                theAttribute.text = valueAliases[theAttribute.text]
                            
                            logger.debug("%s of type %s found key/value of %s=%s in app context %s" % (info["name"], type, attribName, theAttribute.text, app))
                            
                            #Hack to deal with datamodel tables not working as expected
                            if attribName == "description" and type=="datamodels" and info.has_key("dataset.type") and info["dataset.type"] == "table":
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
                                    print "name is " + name
                                    if name != "RootObject":
                                        index = fields.index(field)
                                        del fields[index]
                                
                                res = json.dumps(res)
                                info[attribName] = res
                            #We keep the attributes that are not None
                            elif theAttribute.text:
                                info[attribName] = theAttribute.text
            #If we have not set the keep flag to False
            if keep:
                if nameOverride != "":
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
                
                #Some attributes are not used to create a new version so we remove them...(they may have been used above first so we kept them until now)
                for attribName in fieldIgnoreList:
                    if info.has_key(attribName):
                        del info[attribName]
                
                #Add this to the infoList
                sharing = info["sharing"]
                if not infoList.has_key(sharing):
                    infoList[sharing] = []
                    
                #REST API does not support the creation of null queue entries as tested in 7.0.5 and 7.2.1, these are also unused on search heads anyway so ignoring these with a warning
                if type == "fieldtransformations" and info.has_key("FORMAT") and info["FORMAT"] == "nullQueue":
                    logger.warn("Dropping the transfer of %s of type %s in app context %s with owner %s because nullQueue entries cannot be created via REST API (and they are not required in search heads)" % (info["name"], type, app, info["owner"]))
                else:
                    infoList[sharing].append(info)
                    logger.info("Recording %s info for %s in app context %s with owner %s" % (type, info["name"], app, info["owner"]))
    
    app = destApp
        
    #Cycle through each one we need to migrate, we do global/app/user as users can duplicate app level objects with the same names
    #but we create everything at user level first then re-own it so global/app must happen first
    if infoList.has_key("global"):
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with global level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["global"], destOwner, type, override, app, splunk_rest_dest, endpoint, creationSuccess, creationFailure)

    if infoList.has_key("app"):
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with app level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["app"], destOwner, type, override, app, splunk_rest_dest, endpoint, creationSuccess, creationFailure)
        
    if infoList.has_key("user"):
        logger.debug("Now running runQueriesPerList with knowledge objects of type %s with user (private) level sharing in app %s" % (type, app))
        runQueriesPerList(infoList["user"], destOwner, type, override, app, splunk_rest_dest, endpoint, creationSuccess, creationFailure)
        
    return creationSuccess, creationFailure
    
###########################
#
# runQueriesPerList
#   Runs the required queries to create the knowledge object and then re-owns them to the correct user
# 
###########################
def runQueriesPerList(infoList, destOwner, type, override, app, splunk_rest_dest, endpoint, creationSuccess, creationFailure):
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

        #Doing this update the default nav menu to match the app been transferred
        if type=="navMenu" and name == "default" and override:
            url = "%s/servicesNS/nobody/%s/%s/default" % (splunk_rest_dest, app, endpoint)
            logger.info("Attempting to update %s with name %s on URL %s in app %s" % (type, name, url, app))
            del anInfo["name"]
        else:
            url = "%s/servicesNS/%s/%s/%s" % (splunk_rest_dest, owner, app, endpoint)
            logger.info("Attempting to create %s with name %s on URL %s in app %s" % (type, name, url, app))

        #Hack to handle the times (conf-times) not including required attributes for creation in existing entries
        #not sure how this happens but it fails to create in 7.0.5 but works fine in 7.2.x, fixing for the older versions
        if type=="times (conf-times)" and not payload.has_key("is_sub_menu"):
            payload["is_sub_menu"] = "0"
        
        deletionURL = ""
        logger.debug("Attempting to create %s with name %s on URL %s with payload '%s' in app %s" % (type, name, url, payload, app))
        res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok and res.status_code != 201):
            logger.error("%s of type %s with URL %s status code %s reason %s, response '%s', in app %s, owner %s" % (name, type, url, res.status_code, res.reason, res.text, app, owner))
            creationFailure.append(name)
        else:
            logger.debug("%s of type %s in app %s with URL %s result is: '%s' owner of %s" % (name, type, app, url, res.text, owner))
            
            #Parse the result to find the new URL to use
            root = ET.fromstring(res.text)
            infoList = []
            for child in root:
                #Working per entry in the results
                if child.tag.endswith("entry"):
                    #Down to each entry level
                    for innerChild in child:
                        #print innerChild.tag
                        if innerChild.tag.endswith("link") and innerChild.attrib["rel"]=="remove":
                            deletionURL = "%s/%s" % (splunk_rest_dest, innerChild.attrib["href"])
                            logger.debug("%s of type %s in app %s recording deletion URL as %s" % (name, type, app, deletionURL))
                            creationSuccess.append(deletionURL)
                elif child.tag.endswith("messages"):
                    for innerChild in child:
                        if innerChild.tag.endswith("msg") and innerChild.attrib["type"]=="ERROR" or innerChild.attrib["WARN"]:
                            logger.warn("%s of type %s in app %s had a warn/error message of '%s' owner of %s" % (name, type, app, innerChild.text, owner))
                            #Sometimes the object appears to be create but is unusable which is annoying, at least provide the warning to the logs
                            #and record it in the failure list for investigation
                            creationFailure.append(name)
            if not override:
                #Re-owning it to the previous owner
                url = "%s/acl" % (deletionURL)
                payload = { "owner": owner, "sharing" : sharing }
                logger.info("Attempting to change ownership of %s with name %s via URL %s to owner %s in app %s with sharing %s" % (type, name, url, owner, app, sharing))
                res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
                
                #If re-own fails consider this a failure that requires investigation
                if (res.status_code != requests.codes.ok):
                    logger.error("%s of type %s in app %s with URL %s status code %s reason %s, response '%s', owner of %s" % (name, type, app, url, res.status_code, res.reason, res.text, owner))
                    creationFailure.append(name)
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
                                if innerChild.tag.endswith("link") and innerChild.attrib["rel"]=="remove":
                                    deletionURL = "%s/%s" % (splunk_rest_dest, innerChild.attrib["href"])
                                    logger.debug("%s of type %s in app %s recording new deletion URL as %s , owner is %s and sharing level %s" % (name, type, app, deletionURL, owner, sharing))
                                    #Remove our last recorded URL
                                    if len(creationSuccess) > 1:
                                        creationSuccess.pop()
                                    creationSuccess.append(deletionURL)

###########################
#
# macros
# 
###########################
macroCreationSuccess = []
macroCreationFailure = []

#macro use cases are slightly different to everything else on the REST API
#enough that this code has not been integrated into the runQuery() function
def macros(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
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
                            logger.debug("%s of type macro not in includeEntities list in app %s" % (macroInfo["name"], app))
                            keep = False
                            break
                    if excludeEntities:
                        if title in excludeEntities:
                            logger.debug("%s of type macro in excludeEntities list in app %s" % (macroInfo["name"], app))
                            keep = False
                            break
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
                                elif theList.attrib['name'] == 'app':
                                    logger.debug("macro app: %s" % (theList.text))
                                    foundApp = theList.text
                                    #We can see globally shared objects in our app context, it does not mean we should migrate them as it's not ours...
                                    if app != foundApp:
                                        logging.debug("%s of type macro found in app context of %s, belongs to app context %s, excluding it" % (macroInfo["name"], app, foundApp))
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
                if not macros.has_key(sharing):
                    macros[sharing] = []
                macros[sharing].append(macroInfo)
                logger.info("Recording macro info for %s in app %s with owner %s sharing level of %s" % (macroInfo["name"], app, macroInfo["owner"], macroInfo["sharing"]))
    
    app = destApp

    #Cycle through each one we need to migrate, we do globa/app/user as users can duplicate app level objects with the same names
    #but we create everything at user level first then re-own it so global/app must happen first
    if macros.has_key("global"):
        logger.debug("Now running macroCreation with knowledge objects of type macro with global level sharing in app %s" % (app))
        macroCreation(macros["global"], destOwner, app, splunk_rest_dest, macroCreationSuccess, macroCreationFailure)

    if macros.has_key("app"):
        logger.debug("Now running macroCreation with knowledge objects of type macro with app level sharing in app %s" % (app))
        macroCreation(macros["app"], destOwner, app, splunk_rest_dest, macroCreationSuccess, macroCreationFailure)
        
    if macros.has_key("user"):
        logger.debug("Now running macroCreation with knowledge objects of type macro with user (private) level sharing in app %s" % (app))
        macroCreation(macros["user"], destOwner, app, splunk_rest_dest, macroCreationSuccess, macroCreationFailure)

    return macroCreationSuccess

###########################
#
# macroCreation
#   Runs the required queries to create the macro knowledge objects and then re-owns them to the correct user
# 
###########################

def macroCreation(macros, destOwner, app, splunk_rest_dest, macroCreationSuccess, macroCreationFailure):
    for aMacro in macros:
        sharing = aMacro["sharing"]
        name = aMacro["name"]
        owner = aMacro["owner"]
        
        if destOwner:
            owner = destOwner
            
        url = "%s/servicesNS/%s/%s/properties/macros" % (splunk_rest_dest, owner, app)
        logger.info("Attempting to create macro %s on URL with name %s in app %s" % (name, url, app))

        payload = { "__stanza" : name }
        #Create macro
        #I cannot seem to get this working on the /conf URL but this works so good enough, and it's in the REST API manual...
        #servicesNS/-/search/properties/macros
        #__stanza = <name>
                
        res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok and res.status_code != 201):
            logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s', owner %s" % (name, app, url, res.status_code, res.reason, res.text, owner))
            macroCreationFailure.append(name)
        else:
            #Macros always delete with the username in this URL context
            deletionURL = "%s/servicesNS/%s/%s/configs/conf-macros/%s" % (splunk_rest_dest, owner, app, name)
            logger.debug("%s of type macro in app %s recording deletion URL as %s with owner %s" % (name, app, deletionURL, owner))
            macroCreationSuccess.append(deletionURL)

        logger.debug("%s of type macro in app %s, received response of: '%s'" % (name, app, res.text))
        
        #Now we have created the macro, modify it so it has some real content
        url = "%s/servicesNS/%s/%s/properties/macros/%s" % (splunk_rest_dest, owner, app, name)
        payload = {}
        
        #Remove parts that cannot be posted to the REST API, sharing/owner we change later
        del aMacro["sharing"]
        del aMacro["name"]
        del aMacro["owner"]
        payload = aMacro
        
        logger.debug("Attempting to modify macro %s on URL %s with payload '%s' in app %s" % (name, url, payload, app))
        res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok and res.status_code != 201):
            logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s'" % (name, app, url, res.status_code, res.reason, res.text))
            macroCreationFailure.append(name)
        
        #Re-owning it, I've switched URL's again here but it seems to be working so will not change it
        url = "%s/servicesNS/%s/%s/configs/conf-macros/%s/acl" % (splunk_rest_dest, owner, app, name)
        payload = { "owner": owner, "sharing" : sharing }
        logger.info("Attempting to change ownership of macro %s via URL %s to owner %s in app %s with sharing %s" % (name, url, owner, app, sharing))
        res = requests.post(url, auth=(destUsername,destPassword), verify=False, data=payload)
        if (res.status_code != requests.codes.ok):
            logger.error("%s of type macro in app %s with URL %s status code %s reason %s, response '%s', owner %s sharing level %s" % (name, app, url, res.status_code, res.reason, res.text, owner, sharing))
            #Hardcoded deletion URL as if this fails it should be this URL...(not parsing the XML here to confirm but this works fine)
            deletionURL = "%s/servicesNS/%s/%s/configs/conf-macros/%s" % (splunk_rest_dest, owner, app, name)
            logger.info("%s of type macro in app %s recording deletion URL as user URL due to change ownership failure %s" % (name, app, deletionURL))
            #Remove the old record
            if len(macroCreationSuccess) > 1:
                macroCreationSuccess.pop()
            macroCreationSuccess.append(deletionURL)
        else:
            logger.debug("%s of type macro in app %s, ownership changed with response '%s', new owner %s and sharing level %s" % (name, app, res.text, owner, sharing))

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
def dashboards(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:digest", "eai:userName", "isDashboard", "isVisible", "label", "rootNode", "description" ]
    return runQueries(app, "/data/ui/views", "dashboard", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# Saved Searches
# 
###########################
def savedsearches(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "embed.enabled" ]
    return runQueries(app, "/saved/searches", "savedsearches", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# field definitions
# 
###########################
def calcfields(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute", "type" ]
    aliasAttributes = { "field.name" : "name" }
    return runQueries(app, "/data/props/calcfields", "calcfields", ignoreList, destApp, aliasAttributes, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)
    
def fieldaliases(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute", "type", "value" ]
    return runQueries(app, "/data/props/fieldaliases", "fieldaliases", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

def fieldextractions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute" ]
    return runQueries(app, "/data/props/extractions", "fieldextractions", ignoreList, destApp, {}, { "Inline" : "EXTRACT", "Uses transform" : "REPORT" }, "attribute", destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

def fieldtransformations(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute", "DEFAULT_VALUE", "DEPTH_LIMIT", "LOOKAHEAD", "MATCH_LIMIT", "WRITE_META", "eai:appName", "eai:userName", "DEST_KEY" ]
    return runQueries(app, "/data/transforms/extractions", "fieldtransformations", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)
    
def workflowactions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/data/ui/workflow-actions", "workflow-actions", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

def sourcetyperenaming(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute", "disabled", "eai:appName", "eai:userName", "stanza", "type" ]
    return runQueries(app, "/data/props/sourcetype-rename", "sourcetype-rename", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# tags
# 
##########################
def tags(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/configs/conf-tags", "tags", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# eventtypes
# 
##########################
def eventtypes(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName" ]
    return runQueries(app, "/saved/eventtypes", "eventtypes", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# navMenus
# 
##########################
def navMenu(app, destApp, override, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "eai:digest", "rootNode" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/data/ui/nav", "navMenu", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, override=override, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# data models
# 
##########################
def datamodels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "eai:digest", "eai:type", "acceleration.allowed" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/datamodel/model", "datamodels", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# collections
#
##########################
def collections(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "eai:appName", "eai:userName", "type" ]
    #nobody is the only username that can be used when working with collections
    return runQueries(app, "/storage/collections/config", "collections (kvstore definition)", ignoreList, destApp,destOwner="nobody", noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# viewstates
#
##########################
def viewstates(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "eai:appName", "eai:userName" ]
    #nobody is the only username that can be used when working with collections
    return runQueries(app, "/configs/conf-viewstates", "viewstates", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# time labels (conf-times)
#
##########################
def times(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "header_label" ]
    return runQueries(app, "/configs/conf-times", "times (conf-times)", ignoreList, destApp,destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# panels
#
##########################
def panels(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:digest", "panel.title", "rootNode", "eai:appName", "eai:userName" ]
    return runQueries(app, "/data/ui/panels", "pre-built dashboard panels", ignoreList, destApp,destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)
    
###########################
#
# lookups (definition/automatic)
#
##########################
def lookupDefinitions(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "disabled", "eai:appName", "eai:userName", "CAN_OPTIMIZE", "CLEAN_KEYS", "DEPTH_LIMIT", "KEEP_EMPTY_VALS", "LOOKAHEAD", "MATCH_LIMIT", "MV_ADD", "SOURCE_KEY", "WRITE_META", "fields_array", "type" ]
    #If override we override the default nav menu of the destination app
    return runQueries(app, "/data/transforms/lookups", "datamodels", ignoreList, destApp, destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

def automaticLookups(app, destApp, destOwner, noPrivate, noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, privateOnly):
    ignoreList = [ "attribute", "type", "value" ]
    return runQueries(app, "/data/props/lookups", "automatic lookup", ignoreList, destApp, {}, {}, "attribute", destOwner=destOwner, noPrivate=noPrivate, noDisabled=noDisabled, includeEntities=includeEntities, excludeEntities=excludeEntities, includeOwner=includeOwner, excludeOwner=excludeOwner, privateOnly=privateOnly)

###########################
#
# Logging functions for the output we provide at the end of a migration run
#
##########################

def logDeletionScriptInLogs(list, printPasswords, destUsername, destPassword):
    for item in list:
        item = item.replace("(", "\(").replace(")","\)")
        if printPasswords:
            logger.info("curl -k -u %s:%s --request DELETE %s" % (destUsername, destPassword, item))
        else:
            logger.info("curl -k --request DELETE %s" % (item))

def logCreationFailure(list, type, app):
    for item in list:
        logger.warn("In App %s, %s '%s' failed to create" % (app, type, item))

def printDeletionToConsole(list, printPasswords, destUsername, destPassword):
    for item in list:
        item = item.replace("(", "\(").replace(")","\)")
        if printPasswords:
            print "curl -k -u %s:%s --request DELETE %s" % (destUsername, destPassword, item)
        else:
            print "curl -k --request DELETE %s" % (item)

def logStats(successList, failureList, type, app):
    logger.info("App %s, %d %s successfully migrated %d %s failed to migrate" % (app, len(successList), type, len(failureList), type))

def handleFailureLogging(failureList, type, app):
    logCreationFailure(failureList, type, app)

def logDeletion(successList, printPasswords, destUsername, destPassword):
    logDeletionScriptInLogs(successList, printPasswords, destUsername, destPassword)    
    #printDeletionToConsole(successList, printPasswords, destUsername, destPassword)

###########################
#
# Success / Failure lists
#   these are used later in the code to print what worked, what failed et cetera
#
##########################
savedsearchCreationSuccess = []
savedsearchCreationFailure = []
calcfieldsCreationSuccess = []
calcfieldsCreationFailure = []
fieldaliasesCreationSuccess = []
fieldaliasesCreationFailure = []
fieldextractionsCreationSuccess = []
fieldextractionsCreationFailure = []
dashboardCreationSuccess = []
dashboardCreationFailure = []
fieldTransformationsSuccess = []
fieldTransformationsFailure = []
workflowActionsSuccess = []
workflowActionsFailure = []
sourcetypeRenamingSuccess = []
sourcetypeRenamingFailure = []
tagsSuccess = []
tagsFailure = []
eventtypesSuccess = []
eventtypesFailure = []
navMenuSuccess = []
navMenuFailure = []
datamodelSuccess = []
datamodelFailure = []
lookupDefinitionsSuccess = []
lookupDefinitionsFailure = []
automaticLookupsSuccess = []
automaticLookupsFailure = []
collectionsSuccess = []
collectionsFailure = []
viewstatesSuccess = []
viewstatesFailure = []
timesSuccess = []
timesFailure = []
panelsSuccess = []
panelsFailure = []

#If the all switch is provided, migrate everything
if args.all:
    args.macros = True
    args.tag = True
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

###########################
#
# Run the required functions based on the args
#   Based on the command line parameters actually run the functions which will migrate the knowledge objects
#
##########################
if args.macros:
    logger.info("Begin macros transfer")
    macros(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End macros transfer")

if args.tag:
    logger.info("Begin tags transfer")
    (tagsSuccess, tagsFailure) = tags(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End tags transfer")

if args.eventtypes:
    logger.info("Begin eventtypes transfer")
    (eventtypesSuccess, eventtypesFailure) = eventtypes(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End eventtypes transfer")

if args.calcFields:
    logger.info("Begin calcFields transfer")
    (calcfieldsCreationSuccess, calcfieldsCreationFailure) = calcfields(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End calcFields transfer")

if args.fieldAlias:
    logger.info("Begin fieldAlias transfer")
    (fieldaliasesCreationSuccess, fieldaliasesCreationFailure) = fieldaliases(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End fieldAlias transfer")

if args.fieldTransforms:
    logger.info("Begin fieldTransforms transfer")    
    (fieldTransformationsSuccess, fieldTransformationsFailure) = fieldtransformations(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End fieldTransforms transfer")

if args.fieldExtraction:
    logger.info("Begin fieldExtraction transfer")    
    (fieldextractionsCreationSuccess, fieldextractionsCreationFailure) = fieldextractions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End fieldExtraction transfer")

if args.collections:
    logger.info("Begin collections (kvstore definition) transfer")
    (collectionsSuccess, collectionsFailure) = collections(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End collections (kvstore definition) transfer")

if args.lookupDefinition:
    logger.info("Begin lookupDefinitions transfer")
    (lookupDefinitionsSuccess, lookupDefinitionsFailure) = lookupDefinitions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End lookupDefinitions transfer")

if args.automaticLookup:
    logger.info("Begin automaticLookup transfer")
    (automaticLookupsSuccess, automaticLookupsFailure) = automaticLookups(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End automaticLookup transfer")

if args.times:
    logger.info("Begin times (conf-times) transfer")
    (timesSuccess, timesFailure) = times(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End times (conf-times) transfer")

if args.viewstates:
    logger.info("Begin viewstates transfer")
    (viewstatesSuccess, viewstatesFailure) = viewstates(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End viewstates transfer")
    
if args.panels:
    logger.info("Begin pre-built dashboard panels transfer")
    (panelsSuccess, panelsFailure) = panels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End pre-built dashboard panels transfer")
    
if args.datamodels:
    logger.info("Begin datamodels transfer")
    (datamodelSuccess, datamodelFailure) = datamodels(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End datamodels transfer")

if args.dashboards:
    logger.info("Begin dashboards transfer")
    (dashboardCreationSuccess, dashboardCreationFailure) = dashboards(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End dashboards transfer")    

if args.savedsearches:
    logger.info("Begin savedsearches transfer")
    (savedsearchCreationSuccess, savedsearchCreationFailure) = savedsearches(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End savedsearches transfer")    

if args.workflowActions:
    logger.info("Begin workflowActions transfer")
    (workflowActionsSuccess, workflowActionsFailure) = workflowactions(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End workflowActions transfer")    

if args.sourcetypeRenaming:
    logger.info("Begin sourcetypeRenaming transfer")
    (sourcetypeRenamingSuccess, sourcetypeRenamingFailure) = sourcetyperenaming(srcApp, destApp, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End sourcetypeRenaming transfer")        

if args.navMenu or args.navMenuWithDefaultOverride:
    logger.info("Begin navMenu transfer")
    (navMenuSuccess, navMenuFailure) = navMenu(srcApp, destApp, args.navMenuWithDefaultOverride, destOwner, args.noPrivate, args.noDisabled, includeEntities, excludeEntities, includeOwner, excludeOwner, args.privateOnly)
    logger.info("End navMenu transfer")        

###########################
#
# Logging
#   At the end of all the migration work we log a list of curl commands to "undo" what was migrated
#   we also log failures and stats around the number of successful/failed migrations et cetera
#
##########################
logDeletion(macroCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(tagsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(eventtypesSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(calcfieldsCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(fieldaliasesCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(fieldextractionsCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(fieldTransformationsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(lookupDefinitionsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(automaticLookupsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(viewstatesSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(datamodelSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(dashboardCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(savedsearchCreationSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(workflowActionsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(sourcetypeRenamingSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(navMenuSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(collectionsSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(timesSuccess, args.printPasswords, destUsername, destPassword)
logDeletion(panelsSuccess, args.printPasswords, destUsername, destPassword)

handleFailureLogging(macroCreationFailure, "macros", srcApp)
handleFailureLogging(tagsFailure, "tags", srcApp)
handleFailureLogging(eventtypesFailure, "eventtypes", srcApp)
handleFailureLogging(calcfieldsCreationFailure, "calcfields", srcApp)
handleFailureLogging(fieldaliasesCreationFailure, "fieldaliases", srcApp)
handleFailureLogging(fieldextractionsCreationFailure, "fieldextractions", srcApp)
handleFailureLogging(fieldTransformationsFailure, "fieldtransformations", srcApp)
handleFailureLogging(lookupDefinitionsFailure, "lookupdef", srcApp)
handleFailureLogging(automaticLookupsFailure, "automatic lookup", srcApp)
handleFailureLogging(viewstatesFailure, "viewstates", srcApp)
handleFailureLogging(datamodelFailure, "datamodels", srcApp)
handleFailureLogging(dashboardCreationFailure, "dashboard", srcApp)
handleFailureLogging(savedsearchCreationFailure, "savedsearch", srcApp)
handleFailureLogging(workflowActionsFailure, "workflowactions", srcApp)
handleFailureLogging(sourcetypeRenamingFailure, "sourcetype-renaming", srcApp)
handleFailureLogging(navMenuFailure, "navMenu", srcApp)
handleFailureLogging(collectionsFailure, "collections", srcApp)
handleFailureLogging(timesFailure, "collections", srcApp)
handleFailureLogging(panelsFailure, "collections", srcApp)

logStats(macroCreationSuccess, macroCreationFailure, "macros", srcApp)
logStats(tagsSuccess, tagsFailure, "tags", srcApp)
logStats(eventtypesSuccess, eventtypesFailure, "eventtypes", srcApp)
logStats(calcfieldsCreationSuccess, calcfieldsCreationFailure, "calcfields", srcApp)
logStats(fieldaliasesCreationSuccess, fieldaliasesCreationFailure, "fieldaliases", srcApp)
logStats(fieldextractionsCreationSuccess, fieldextractionsCreationFailure, "fieldextractions", srcApp)
logStats(fieldTransformationsSuccess, fieldTransformationsFailure, "fieldtransformations", srcApp)
logStats(lookupDefinitionsSuccess, lookupDefinitionsFailure, "lookupdef", srcApp)
logStats(automaticLookupsSuccess, automaticLookupsFailure, "automatic lookup", srcApp)
logStats(viewstatesSuccess, viewstatesFailure, "viewstates", srcApp)
logStats(datamodelSuccess, datamodelFailure, "datamodels", srcApp)
logStats(dashboardCreationSuccess, dashboardCreationFailure, "dashboard", srcApp)
logStats(savedsearchCreationSuccess, savedsearchCreationFailure, "savedsearch", srcApp)
logStats(workflowActionsSuccess, workflowActionsFailure, "workflowactions", srcApp)
logStats(sourcetypeRenamingSuccess, sourcetypeRenamingFailure, "sourcetype-renaming", srcApp)
logStats(navMenuSuccess, navMenuFailure, "navMenu", srcApp)
logStats(collectionsSuccess, collectionsFailure, "collections", srcApp)
logStats(timesSuccess, timesFailure, "times (conf-times)", srcApp)
logStats(panelsSuccess, panelsFailure, "pre-built dashboard panels", srcApp)

logging.info("The undo command is: grep -o \"curl.*DELETE.*\" /tmp/transfer_knowledgeobj.log | grep -v \"curl\.\*DELETE\"")
logging.info("Done")
