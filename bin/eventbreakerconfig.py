import os
import re
import errno
import argparse
import logging
from logging.config import dictConfig

###############################
#
# What does this script do?
#
###############################
# Attempts to browse through a directory to find all props.conf files
# if a props.conf file is found then searches for a LINE_BREAKER
# if the LINE_BREAKER exists creates an EVENT_BREAKER version in the new finalOutputDir
# 
# Note that the directories are relative so if the props.conf is in /opt/splunk/etc/deployment-apps/Splunk_TA_windows/default/props.conf
# then the output dir is finalOutputDir/Splunk_TA_windows/default/props.conf
#
# Only the relevant stanzas are included that contain a LINE_BREAKER and every item found is renamed EVENT_BREAKER
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
              'filename' : '/tmp/event_breaker_config.log',
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

parser = argparse.ArgumentParser(description='Based on a directory containing Splunk apps, extract out all LINE_BREAKER entries and re-create as EVENT_BREAKER, also find stanzas using SHOULD_LINEMERGE=false to ensure EVENT_BREAKER_ENABLE is output to the output directory. Outputs directories with the same name as the input directories')
parser.add_argument('-srcDir', help='Splunk application directory containing dirs with props.conf files', required=True)
parser.add_argument('-destDirRoot', help='The directory where the output directory names should be created, the subdirectories will have the same application names as found in the srcDir', required=True)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')

args = parser.parse_args()

if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

path = args.srcDir
finalOutputDir = args.destDirRoot

#Empty dictionary of what we need
appDirsRequired = {}

#List only directories and not files under a particular directory
def listdirs(dir):
    return [d for d in os.listdir(dir) if os.path.isdir(os.path.join(dir, d))]
    
dirList = listdirs(path)

#Determine which directories have props.conf files and then parse them later
for dir in dirList:
    #Expecting default/local at this level
    relativepath = path + "/" + dir
    subdir = listdirs(relativepath)
    for configDir in subdir:
        relativeconfigdir = relativepath + "/" + configDir
        files = os.listdir(relativeconfigdir)
        for file in files:
            if file=="props.conf":
                if not appDirsRequired.has_key(relativepath):
                    appDirsRequired[relativepath] = []
                appDirsRequired[relativepath].append(relativeconfigdir)
                logger.debug("Adding file %s from dir %s" % (file, relativeconfigdir))

#We use these regex'es later to find the start of any [source type name] or LINE_BREAKER lines
regex1 = re.compile("^\s*(\[)(.*)")
regex2 = re.compile("^\s*LINE_BREAKER(.*)")
regex3 = re.compile("^\s*SHOULD_LINEMERGE\s*=\s*([^ \r\n]+)\s*")
regex4 = re.compile("[^ =\t]")
outputFiles = {}

#Cycle through the list that have props files so we can work on them
for adir in appDirsRequired.keys():
    subdirs = appDirsRequired[adir]
    
    #At this point we're dealing with the default or local directory
    for dir in subdirs:
        #if it was in the list we had a props.conf to deal with
        aFile = dir + "/props.conf"

        logger.info("Open file %s" % (aFile))

        #open the file read only and work with it
        with open(aFile) as file:
            currentStanza = ""            
            first = True
            linebreakerfound = False
            eventbreaker = False
           
            fulldir = ""
           
            for line in file:
                #determine if this is a stanza entry
                result = regex1.match(line)
                if result != None:
                    #It is possible we have gone through this loop before and we're now looking at a new stanza
                    #it's possible we have some unfinished business, such as what if SHOULD_LINEMERGE=false but we didn't
                    #have a LINE_BREAKER, this is harmless as EVENT_BREAKER has a default but it prints annoying messages such as:
                    #INFO  ChunkedLBProcessor - Failed to find EVENT_BREAKER regex in props.conf for sourcetype::thesourcetypename. Reverting to the default EVENT_BREAKER regex for now 
                    #to avoid this we explictly put in the EVENT_BREAKER if it isn't there
                    if eventbreaker and not linebreakerfound:
                        outputFiles[fulldir].append("EVENT_BREAKER = ([\\r\\n]+)")
                    #this scenario should ideally not occur, but it happens so print a warning
                    elif linebreakerfound and not eventbreaker:
                        logger.warn("For %s in %s there was a LINE_BREAKER but SHOULD_LINEMERGE not set to false therefore it won't work as expected. Fix it!" % (currentStanza, fulldir))

                    #Add a newline to the end of the last stanza
                    if currentStanza != "" and fulldir != "":
                        outputFiles[fulldir].append("")                        
                        
                    stanza = result.group(1) + result.group(2)
                    currentStanza = stanza
                    logger.debug("Working with stanza %s in file %s" % (stanza, aFile))
                    first = True
                    linebreakerfound = False
                    eventbreaker = False
                    continue
                
                
                #If we find the LINE_BREAKER line then we need to include this stanza into the output files we're creating
                result = regex2.match(line)
                result2 = regex3.match(line)
                if result != None or result2 != None:
                    #We know the directory looks something like
                    #/opt/splunk/etc/deployment-apps/Splunk_TA_windows/default
                    #We use the basename to determine if it's local or default
                    #then we use dirname + basename to determine the app name above
                    curbase = os.path.basename(dir)
                    parent = os.path.dirname(dir)
                    relativedir = os.path.basename(parent)
 
                    fulldir = relativedir + "/" + curbase
                    
                    #We keep a list of output lines per-file
                    if not outputFiles.has_key(fulldir):
                        outputFiles[fulldir] = []
                    
                    res = ""
                    if result != None:
                        linebreakerfound = True
                        #We re-use the LINE_BREAKER stanza but we call it EVENT_BREAKER now
                        #However due to bug SPL-159337 we remove non-capturing regexes as they are not supported
                        eventbreakerstr = result.group(1).replace("?:","")
                        #Furthermore not all of our existing LINE_BREAKER's include a capturing group, the LINE_BREAKER can handle this
                        #the event breaker segfaults
                        result4 = regex4.search(eventbreakerstr)
                        eventbreakerstr = eventbreakerstr[0:result4.start()] + " (" + eventbreakerstr[result4.start():] + ")"
                        res = "EVENT_BREAKER " + eventbreakerstr
                    else:                        
                        res = result2.group(1).lower()
                        #if SHOULD_LINEMERGE = false then we can enable the event breaker, if not we cannot
                        if res == "false" or res == "0":
                            res = "EVENT_BREAKER_ENABLE = true"
                            eventbreaker = True
                        else:
                            continue
                    
                    if first:
                        outputFiles[fulldir].append("%s" % (currentStanza))
                        first = False
                                        
                    outputFiles[fulldir].append("%s" % (res))                                          
            
            #it is possible there is only 1 stanza in the file so the file ends and we didn't get to write out the EVENT_BREAKER= line
            if eventbreaker and not linebreakerfound:
                outputFiles[fulldir].append("EVENT_BREAKER = ([\\r\\n]+)")
            #this scenario should ideally not occur, but it happens so print a warning
            elif linebreakerfound and not eventbreaker:
                logger.warn("For %s in %s there was a LINE_BREAKER but SHOULD_LINEMERGE not set to false therefore it won't work as expected. Fix it!" % (currentStanza, fulldir))

#We now have a list of outputFiles that we can output
for outputDir in outputFiles.keys():
    #Debugging info
    #print "%s contents %s" % (outputDir, outputFiles[outputDir])
    
    finaldir = finalOutputDir + "/" + outputDir
    
    #make the relative directory for example /tmp/Splunk_TA_windows/default or similar
    logging.debug("Creating directory in %s" % (finaldir))
    try:
        os.makedirs(finaldir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    
    #output the props.conf and all lines that we have
    outputH = open(finaldir + "/props.conf", "w")
    for aLine in outputFiles[outputDir]:
        outputH.write(aLine + "\n")
