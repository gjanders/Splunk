import re
import getpass
import sys
import os
import indextuning_utility as idx_utility
import indextuning_indextempoutput
import indextuning_dirchecker
import datetime
import shutil
import argparse
import logging
from logging.config import dictConfig
import requests

###############################
#
# What does this script do?
#
###############################
# Attempts to use the output of the command:
# splunk btool indexes list --debug
# To determine all indexes configured on this Splunk indexer and then provides tuning based on the previously seen license usage & bucket sizing
# in addition this script provides a list of directories which are located in the same directory as hot, cold or tstatsHomePath and if there is no matching
# index configuration suggests deletion via an output file
# 
# The script takes no input, the script outputs copies of the relevant index configuration files with suggested tuning within the file so a diff command can be used to 
# compare the output
#
# In more depth:
# Index bucket sizing (maxDataSize setting)
#   * Based on output of dbinspect command determining how many hours worth of data will fit into a bucket
#   * Based on target number of hours determines if the bucket sizing is currently appropriate
#   * Based on recent license usage, compression ratio, number of indexers/buckets approx if the license usage seems reasonable or not as a sanity check
#   *   the above sanity check can cause issues *if* a very small amount of data exists in the index, resulting in compression ratios of 500:1 (500 times larger on disk)
#   *   minSizeToCalculate exists to prevent this from happening
#   * If bucket size is auto_high_volume, and tuned size*contingency is less than 750MB, then auto will be suggested
#   * If bucket size is auto tuned, auto_high_volume will be suggested if tuned size * contingency is larger than 750MB
#   * If bucket was using a size-based maxDataSize then the new value will be suggested (not recommended, auto/auto_high_volume are usually fine)
#
# Index sizing (maxTotalDataSizeMB)
#   * Based on the last X days of license usage (configurable) and the storage compression ratio based on the most recent introspection data 
#     and multiplied by the number of days before frozen, multiplied by contingency and divided by number of indexers to determine value per indexer
#   * If the index has recent license usage *but* it does not match the minimum number of days we require for sizing purposes we do not size the index
#     this covers, for example an index that has 2 days of license usage and it's too early to correctly re-size it
#   * If the index has no data on the filesystem no tuning is done but this is recorded in the log (either a new index or an unused index)
#   * If the index has zero recent license usage over the measuring period *and* it does not have a sizing comment then we cap it at the 
#     current maximum size * contingency with a lower limit of lowerIndexSizeLimit, this parameter prevents bucket explosion from undersizing indexes
#   * If the index has zero recent license usage over the measuring period *and* it does have a sizing comment then we do not change it
#   * If the index has recent license usage and it is oversized, in excess of the percBeforeAdjustment then we adjust (oversized index)
#   * If the index has recent license usage and it is undersized (including a % contingency we add), we adjust (undersized index) currently we do this even if sizing comments exist
#     note that this particular scenario can result in the index becoming larger than the original sizing comment, it is assumed that data loss should be avoided by the script
#   * Finally if oversized we sanity check that we are not dropping below the largest on-disk size * contingency on any indexer as that would result in deletion of data
#
# TODO an indexes datatype can be event or metric, if metric do we do the same tuning? 
# Since dbinspect appears to work along with other queries assuming they can be treated the same for now...

#In addition to console output we write the important info into a log file
#console output for debugging purposes only
outputLogFile = "/tmp/indextuning.log"

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
              'filename' : outputLogFile,
              'formatter': 'f',
              'maxBytes' :  2097152,
              'level': logging.DEBUG,
              'backupCount': 5 }
        },        
    root = {
        'handlers': ['h','file'],
        'level': logging.DEBUG,
        },
)

dictConfig(logging_config)

logger = logging.getLogger(__name__)

#Create the argument parser
parser = argparse.ArgumentParser(description='Re-tune the indexes.conf files of the current indexer based on passed in tuning parameters and an estimation of what is required')

#Useful for testing only, should be set to unlimited by default, limit setting
parser.add_argument('-indexLimit', help='Number of indexes to run through before tuning is stopped at that point in time', default=9999, type=int)
parser.add_argument('-destURL', help='URL of the REST/API port of the Splunk instance, localhost:8089 for example', default="localhost:8089:8089")

#How far to go back in queries related to bucket information
parser.add_argument('-earliesttime', help='Earliest point in time to find bucket information', default="-16d")

#Earliest time is 14 days ago and we start at midnight to ensure we get whole days worth of license usage
parser.add_argument('-earliestLicense', help='Earliest point in time to find license details (use @d to ensure this starts at midnight)', default="-30d@d")
parser.add_argument('-latestLicense', help='Latest point in time to find license details (use @d to ensure this ends at midnight)', default="@d")
parser.add_argument('-numberOfIndexers', help='Number of indexers the tuning should be based on', default="10", type=int)

#Aim for 24 hours per bucket for now, we add contingency to this anyway
parser.add_argument('-numHoursPerBucket', help='Aim for approximate number of hours per bucket (not including contingency)', default="24")

#Add 20% contingency to the result for buckets
parser.add_argument('-bucketContingency', help='Contingency multiplier for buckets', default=1.2, type=float)

#Add 35% contingency to the result for index sizing purposes
parser.add_argument('-sizingContingency', help='Contingency multiplier for index sizing', default=1.35, type=float)

#What % do we leave spare in our indexes before we increase the size? 20% for now
#Note an additional safety that checks the max on disk size per index can also override this
#By having the sizingContingency larger than this setting we hope that re-sizing will rarely occur once increased correctly
#as it would have to change quite a bit from the average...
#parser.add_argument('-undersizingContingency', help='Contingency multiplier before an index is increased (i.e. 1.2 is if the index is undersized by less than 20% do nothing)', default=1.2, type=float)
parser.add_argument('-undersizingContingency', help='Contingency multiplier before an index is increased (1.2 is if the index is undersized by less than 20 perc. do nothing)', default=1.2, type=float)

#What host= filter do we use to find index on-disk sizing information for the compression ratio?
parser.add_argument('-indexerhostnamefilter', help='Host names for the indexer servers for determining size on disk queries', default="*")

#default as in the [default] entry appears in the btool output but requires no tuning
parser.add_argument('-indexIgnoreList', help='List of indexes that tuning should not be attempted on. CSV separated list without spaces', default="_internal,_audit,_telemetry,_thefishbucket,_introspection,history,default,splunklogger")

#What's the smallest index size we should go to, to avoid bucket explosion? At least maxHotBuckets*maxDataSize + some room? Hardcoded for now
parser.add_argument('-lowerIndexSizeLimit', help='Minimum index size limit to avoid bucket explosion', default=3000, type=int)

#What's the minimum MB we can leave for a bucket in cold for a really small index?
parser.add_argument('-smallBucketSize', help='Minimum cold section size for a small bucket', default=800, type=int)

#If the index is oversized by more than this % then adjustments will occur
#this prevents the script from making small adjustments to index sizing for no reason, it has to be more than 20% oversized
#if we utilise 0.8 as the percBeforeAdjustment
#If undersized we always attempt to adjust immediately
parser.add_argument('-percBeforeAdjustment', help='Multiplier for downsizing an oversized index (0.8 means that the index must be more than 20 perc. oversized for any adjustment to occur)', default=0.8, type=float)

#Path to store temporary files such as the indexes.conf outputs while this is running
parser.add_argument('-workingPath', help='Path used for temporary storage of index.conf output', default="/tmp/indextuningtemp")

#Number of days of license data required for sizing to occur, if we don't have this many days available do not attempt to size
parser.add_argument('-minimumDaysOfLicenseForSizing', help="Number of days of license data required for sizing to occur, if we don't have this many days available do not attempt to resize", default=15, type=int)
#How many MB of data should we have before we even attempt to calculate bucket sizing on a per indexer basis?
#where a number too small results in a giant compression ratio where stored size > raw data
parser.add_argument('-minSizeToCalculate', help="Minimum bucket size before caluation is attempted", default=100.0, type=float)
#Exclude default directories from /opt/splunk/var/lib/splunk and should not be deleted as they won't exist in the indexes.conf files
parser.add_argument('-excludedDirs', help="List of default directories to exclude from been listed in the deletion section", default='kvstore,.snapshots,lost+found,authDb,hashDb,persistentstorage')
#If the compression ratio is above this level we throw a warning
parser.add_argument('-upperCompRatioLevel', help="Comp ratio limit where a warning in thrown rather than calculation done on the bucket sizing if this limit is exceeded", default=6.0, type=float)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-username', help='Username to login to the remote Splunk instance with', required=True)
parser.add_argument('-password', help='Password to login to the remote Splunk instance with', required=True)
parser.add_argument('-indexNameRestriction', help='List of index names to run against (defaults to all indexes)')
parser.add_argument('-deadIndexCheck', help='Only use the utility to run the dead index check, no index tuning required', action='store_true')
parser.add_argument('-deadIndexDelete', help='After running the dead index check perform an rm -R on the directories which appear to be no longer in use, use with caution', action='store_true')
parser.add_argument('-doNotLoseData', help='By default the index sizing estimate overrides any attempt to prevent data loss, use this switch to provide extra storage to prevent data loss (free storage!)', action='store_true')
parser.add_argument('-sizingEstimates', help='Only run sizing estimates (do not resize indexes)', action='store_true')
parser.add_argument('-indexTuning', help='Only run index tuning (resize indexes + buckets)', action='store_true')
parser.add_argument('-bucketTuning', help='Only run index tuning (resize buckets only)', action='store_true')
parser.add_argument('-indexSizing', help='Only run index tuning (resize indexes only)', action='store_true')
parser.add_argument('-all', help='Run index sizing and sizing estimates, along with a list of unused indexes', action='store_true')
parser.add_argument('-useIntrospectionData', help='Use introspection data rather than the REST API data for index bucket calculations, slightly faster but earliest time may not be 100 percent accurate (data is likely older than logged)', action='store_true')
parser.add_argument('-workWithGit', help='Check changes back into git', action='store_true')
parser.add_argument('-gitWorkingDir', help='Directory to perform git clone / pull / commits in', default="/tmp/indextuning_git_temp")
parser.add_argument('-gitFirstConnection', help='Run an SSH command to add the git repo to the list of trusted SSH fingerprints')
parser.add_argument('-gitRepoURL', help='URL of the git repository (using an SSH key)')
parser.add_argument('-gitBranch', help='git branch prefix to use to create to send to remote repo', default="indextuning")
parser.add_argument('-gitRoot', help='What is the directory under the git repository where the master-apps directory sits?', default="")
parser.add_argument('-gitLabToken', help='The gitLab private token or access token, if supplied a merge request is created using this token')
parser.add_argument('-gitLabURL', help='URL of the remote gitlab server to work with', default="")
parser.add_argument('-outputTempFilesWithTuning', help='Output files into the working path with tuning results (optional)', action='store_true')

#Skip indexes where the size on disk is greater than the estimated size (i.e. those who have serious balance issues or are exceeding usage expectations)
parser.add_argument('-skipProblemIndexes', help='Skip re-sizing attempts on the index size for any index perceived as a problem (for example using more disk than expected or similar)', action='store_true')

###############################
#
# Initial setup
#
###############################

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

args.indexIgnoreList = args.indexIgnoreList.split(",")
args.excludedDirs = args.excludedDirs.split(",")

if args.all:
    args.sizingEstimates = True
    args.indexTuning = True
    args.deadIndexCheck = True
    
if args.indexTuning:
    args.bucketTuning = True
    args.indexSizing = True

#helper function as per https://stackoverflow.com/questions/31433989/return-copy-of-dictionary-excluding-specified-keys
def without_keys(d, keys):
    return {x: d[x] for x in d if x not in keys}

#Keep a large dictionary of the indexes and associated information
indexList = {}

#Setup the utility class with the username, password and URL we have available
logger.debug("Creating utility object with username %s, destURL %s, earliest time %s" % (args.username, args.destURL, args.earliesttime))
utility = idx_utility.utility(args.username, args.password, args.destURL, args.earliesttime)

#Run /opt/splunk/bin/splunk btool and determine the index/volume list we are working with
indexList, volList = utility.parseBtoolOutput()

#We only care about unique dirs to check and unique directories that are unused
deadIndexDirList = {}
deadSummaryDirList = {}

#Cleanup previous runs
if (os.path.isdir(args.workingPath)):
    logger.debug("Deleting old directory %s after previous run" % (args.workingPath))
    shutil.rmtree(args.workingPath)

excludedList = [ "password", "gitLabToken" ]
cleanArgs = without_keys(vars(args), excludedList)
logger.info("Begin index sizing script with args %s" % (cleanArgs))

###############################
#
# Check for directories on the filesystem that do not appear to be related to any index
#
###############################
#Determine locations that exist on the filesystem, and if they don't exist in the list from the btool output
#they are likely dead index directories from indexes that were once here but now removed
#TODO this could be a function and moved outside the main code
#Note this is here because later we modify the indexList to remove ignored indexes
#where now we want them in the list to ensure we do not attempt to delete in use directories!

#the checkdirs function returns the top level directories from the indexes.conf which we should be checking, for example /opt/splunk/var/lib/splunk
#Should not be passing around objects but this will do for now
#TODO fix this
indexDirCheckRes = False
if args.deadIndexCheck:
    indexDirCheckRes = indextuning_dirchecker.checkForDeadDirs(indexList, volList, args.excludedDirs, utility, logging)
    
###############################
#
# Begin main logic section
#
###############################
#Shared functions required by both index tuning/sizing & bucket tuning/sizing
def indexTuningPresteps(utility, indexList, indexIgnoreList, earliestLicense, latestLicense, indexNameRestriction, indexLimit, indexerhostnamefilter, useIntrospectionData):
    confFilesToCheck = {}
    for index in indexList.keys():
        confFile = indexList[index]["confFile"]
        #ignore known system files that we should not touch
        #TODO default to using a local file equivalent for non-system directories
        if confFile.find("/etc/system/default/") == -1 and confFile.find("_cluster/default/") == -1:
            confFilesToCheck[confFile] = True

    #parse all the conf files and look for comments about sizing (as this overrides settings later in the code)
    logger.debug("Running parseConfFilesForSizingComments()")
    
    #This just updates the indexList dictionary with new data
    utility.parseConfFilesForSizingComments(indexList, confFilesToCheck)
    
    #At this point we have indexes that we are supposed to ignore in the dictionary, we need them there so we could 
    #ensure that we didn't suggest deleting them from the filesystem, however now we can ignore them so we do not
    #attempt to re-size the indexes with no license info available
    logger.debug("The following indexes will be ignored as per configuration %s" % (indexIgnoreList))
    for index in indexIgnoreList:
        if indexList.has_key(index):
            del indexList[index]
            logger.debug("Removing index %s from indexList" % (index))
            
    counter = 0
    indexCount = len(indexList)

    if (indexLimit < indexCount):
        indexCount = indexLimit

    for indexName in indexList.keys():
        #If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if indexNameRestriction:
            if indexName != indexNameRestriction:
                continue
        #useful for manual runs
        logger.debug("%s iteration of %s within indexLoop" % (counter, indexCount))
        
        #If we're performing a limited run quit the loop early when we reach the limit
        if (counter > indexLimit):
            break
        
        #Actually check license usage per index over the past X days, function returns three ints
        logger.info("Index: %s, running determineLicenseUsagePerDay" % (indexName))
        indexList[indexName]["avgLicenseUsagePerDay"], indexList[indexName]["firstSeen"], indexList[indexName]["maxLicenseUsagePerDay"] = utility.determineLicenseUsagePerDay(indexName, earliestLicense, latestLicense)
        
        #Determine compression ratio of each index, function returns floats, compRatio is re-used during index sizing so required by both index sizing
        #and bucket sizing scenarios
        logger.info("Index: %s, running determineCompressionRatio" % (indexName))
        indexList[indexName]["compRatio"], indexList[indexName]["curMaxTotalSize"], indexList[indexName]["earliestTime"] = utility.determineCompressionRatio(indexName, indexerhostnamefilter, useIntrospectionData)

        counter = counter + 1

#Functions exlusive to bucket sizing
def runBucketSizing(utility, indexList, indexNameRestriction, indexLimit, numHoursPerBucket, bucketContingency, upperCompRatioLevel, minSizeToCalculate, numberOfIndexers):
    todaysDate = datetime.datetime.now().strftime("%Y-%m-%d")

    counter = 0
    auto_high_volume_sizeMB = 10240
    indexCount = len(indexList)

    if (indexLimit < indexCount):
        indexCount = indexLimit
    
    logger.info("Running queries to determine bucket sizing, licensing, et cetera")
    #Actually run the various Splunk query functions to find bucket sizing, compression ratios and license usage
    for indexName in indexList.keys():
        #If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if indexNameRestriction:
            if indexName != indexNameRestriction:
                continue
        #useful for manual runs
        logger.debug("%s iteration of %s within indexLoop" % (counter, indexCount))
        
        #If we're performing a limited run quit the loop early when we reach the limit
        if (counter > indexLimit):
            break
        #function returns a float for recBucketSize
        logger.info("Index: %s, running determineRecommendedBucketSize" % (indexName))
        
        indexList[indexName]['recBucketSize'] = utility.determineRecommendedBucketSize(indexName, numHoursPerBucket) 
        #Add % bucketContingency to bucket sizing
        indexList[indexName]['recBucketSize'] = indexList[indexName]['recBucketSize'] * bucketContingency 
        
        #If we have run the required checks on the index mark it True, otherwise do not, this is used later and relates to the limited index runs
        indexList[indexName]["checked"] = True
        counter = counter + 1

    #Keep a dictionary of indexes requiring changes and conf files we need to output
    indexesRequiringChanges = {}
    confFilesRequiringChanges = []

    counter = 0
    logger.info("Now running bucket sizing calculations")
    for indexName in indexList.keys():
        logger.debug("Working on index name %s with counter %s" % (indexName, counter))
        #If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if indexNameRestriction:
            if indexName != indexNameRestriction:
                continue
        
        #If we're performing a limited run quit the loop early when we reach the limit
        if (counter > indexLimit):
            break
        counter = counter + 1
        
        maxHotBuckets = indexList[indexName]['maxHotBuckets']
        bucketSize = indexList[indexName]['maxDataSize']
        confFile = indexList[indexName]['confFile']
        recommendedBucketSize = indexList[indexName]['recBucketSize']
        indexList[indexName]['numberRecBucketSize'] = indexList[indexName]['recBucketSize']
        storageRatio = indexList[indexName]["compRatio"] 
        curMaxTotalSize = indexList[indexName]["curMaxTotalSize"]
        earliestTime = indexList[indexName]["earliestTime"]
        maxTotalDataSizeMB = float(indexList[indexName]['maxTotalDataSizeMB'])
        
        #If the compression ratio is unusually large warn but continue for now
        if (storageRatio > upperCompRatioLevel):
            logger.info("Index: %s, returned index compression ratio %s , this is above the expected max of %s, this may break calculations in the script capping it at %s" % (indexName, storageRatio, upperCompRatioLevel, upperCompRatioLevel))
            storageRatio = upperCompRatioLevel
        
        #If we have a really, really small amount of data such as hundreds of kilobytes the metadata can be larger than the raw data resulting in a compression ratio of 500
        #(i.e. the stored size is 500 times larger on disk than it is in raw data, resulting in other calculations such as bucket sizing getting broken
        #the alternative size calculation is used for this reason, and if the data is too small to calculate we use an upper bound on the ratio as a safety
        if (curMaxTotalSize > minSizeToCalculate):
            #If the data is poorly parsed (e.g. dates go well into the past) then the MB/day might be greater than what appears via dbinspect
            #and therefore we might need to sanity check this based on license usage * storage ratio / number of indexers / (potential hot buckets)
            #we add contingency to this as well
            altBucketSizeCalc = ((indexList[indexName]["maxLicenseUsagePerDay"] * storageRatio) / numberOfIndexers) / maxHotBuckets 
            altBucketSizeCalc = altBucketSizeCalc * bucketContingency
            
            if (altBucketSizeCalc > recommendedBucketSize):
                logger.info("Index: %s alternative bucket size calculation showed %s, original size %s, new bucket sizing based on %s" % (indexName, altBucketSizeCalc, recommendedBucketSize, altBucketSizeCalc))
                recommendedBucketSize = altBucketSizeCalc
                indexList[indexName]['recBucketSize'] = recommendedBucketSize
                indexList[indexName]['numberRecBucketSize'] = recommendedBucketSize
        else:
            logger.info("Index: %s had a comp ratio of %s and a total size of %s, this is less than the lower bound of %s, not performing the alternative bucket size calculation, oldest data found is %s days old" % (indexName, storageRatio, curMaxTotalSize, minSizeToCalculate, earliestTime))
        #We only change values where required, otherwise we output the line as we read it
        requiresChange = False
        #If we didn't auto tune the bucket and it's a lot smaller or bigger than we change the values to the new numbers
        if bucketSize.find("auto") == -1:
            logger.warn("Not an auto sized bucket for index: " + indexName + " this index should probably be excluded from sizing")
        #We are using auto-tuned bucket size'es so let's keep this using an auto value
        else:
            #It is an auto sized bucket, this makes it slightly different
            logger.debug("Auto sized bucket for index: %s with size %s" % (indexName, bucketSize)) 
            end = bucketSize.find("_")
            #With auto sized buckets we probably care more when the buckets are too small rather than too large (for now)
            bucketAutoSize = float(bucketSize[0:end])
            percDiff = (100 / bucketAutoSize)*recommendedBucketSize
            #If we expect to exceed the auto size in use, go to the auto_high_volume setting, assuming we are not already there
            if (percDiff > 100 and not bucketSize == "10240_auto"):
                homePathMaxDataSizeMB = indexList[indexName]['homePathMaxDataSizeMB']
                
                logger.debug("homepath size is %s and auto_high_volume_sizeMB * maxHotBuckets is %s and %s or maxTotal %s" % (homePathMaxDataSizeMB, auto_high_volume_sizeMB* maxHotBuckets, homePathMaxDataSizeMB, maxTotalDataSizeMB))
                if homePathMaxDataSizeMB != 0.0 and (auto_high_volume_sizeMB * maxHotBuckets) > homePathMaxDataSizeMB:
                    logger.warn("Index: %s would require a auto_high_volume (10GB) bucket but the homePathMaxDataSizeMB of size %s cannot fit %s buckets of that size, not changing the bucket sizing" % (indexName, homePathMaxDataSizeMB, maxHotBuckets))
                elif homePathMaxDataSizeMB == 0.0 and (auto_high_volume_sizeMB * maxHotBuckets) > maxTotalDataSizeMB:
                    logger.warn("Index: %s would require a auto_high_volume (10GB) bucket but the maxTotalDataSizeMB of size %s cannot fit %s buckets of that size, not changing the bucket sizing" % (indexName, maxTotalDataSizeMB, maxHotBuckets))
                else:
                    requiresChange = "bucket" 
                    #If we don't have any change comments so far create the dictionary
                    if (not indexList[indexName].has_key('changeComment')):
                        indexList[indexName]['changeComment'] = {}
                    #Write comments into the output files so we know what tuning occured and when
                    indexList[indexName]['changeComment']['bucket'] = "# Bucket size increase required estimated %s, auto-tuned on %s\n" % (indexList[indexName]["numberRecBucketSize"], todaysDate)
                    #Simplify to auto_high_volume
                    indexList[indexName]['recBucketSize'] = "auto_high_volume"
                    logger.info("Index: %s , file %s , current bucket size is auto tuned to %s , calculated bucket size %s (will be set to auto_high_volume (size increase)), maxHotBuckets %s" % (indexName, confFile, bucketSize, recommendedBucketSize, maxHotBuckets))
            else:
                #Bucket is smaller than current sizing, is it below the auto 750MB default or not, and is it currently set to a larger value?
                if (recommendedBucketSize < 750 and bucketAutoSize > 750):
                    requiresChange = "bucket" 
                    #If we don't have any change comments so far create the dictionary
                    if (not indexList[indexName].has_key('changeComment')):
                        indexList[indexName]['changeComment'] = {}
                        
                    #Write comments into the output files so we know what tuning occured and when
                    indexList[indexName]['changeComment']['bucket'] = "# Bucket size decrease required estimated %s, auto-tuned on %s\n" % (indexList[indexName]["numberRecBucketSize"], todaysDate)
                    indexList[indexName]['recBucketSize'] = "auto"
                    logger.info("Index: %s , file %s , current bucket size is auto tuned to %s , calculated bucket size %s (will be set to auto (size decrease)), maxHotBuckets %s" % (indexName, confFile, bucketSize, recommendedBucketSize, maxHotBuckets))
                    
        #If this index requires change we record this for later
        if (requiresChange != False):
            indexesRequiringChanges[indexName] = requiresChange
            logger.debug("Index %s requires changes of %s" % (indexName, requiresChange))
        
            #Add the conf file to the list we need to work on
            if (not confFile in confFilesRequiringChanges):
                confFilesRequiringChanges.append(confFile)
                logger.debug("Index: %s resulted in file %s added to change list" % (indexName, confFile))

    return indexesRequiringChanges, confFilesRequiringChanges

#Determine which conf files should be read...
def runIndexSizing(utility, indexList, indexNameRestriction, indexLimit, numberOfIndexers, lowerIndexSizeLimit, sizingContingency, minimumDaysOfLicenseForSizing, percBeforeAdjustment, doNotLoseData, undersizingContingency, smallBucketSize, skipProblemIndexes, indexesRequiringChanges, confFilesRequiringChanges):
    todaysDate = datetime.datetime.now().strftime("%Y-%m-%d")

    counter = 0
    indexCount = len(indexList)

    if (indexLimit < indexCount):
        indexCount = indexLimit
    
    calcSizeTotal = 0

    counter = 0
    logger.info("Now running storage sizing calculations")
    for indexName in indexList.keys():
        logger.debug("Working on index name %s with counter %s" % (indexName, counter))
        #If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if indexNameRestriction:
            if indexName != indexNameRestriction:
                continue
        
        #If we're performing a limited run quit the loop early when we reach the limit
        if (counter > indexLimit):
            break
        counter = counter + 1
        
        #maxHotBuckets = indexList[indexName]['maxHotBuckets']
        #bucketSize = indexList[indexName]['maxDataSize']
        confFile = indexList[indexName]['confFile']
        #recommendedBucketSize = indexList[indexName]['recBucketSize']
        #indexList[indexName]['numberRecBucketSize'] = indexList[indexName]['recBucketSize']
        storageRatio = indexList[indexName]["compRatio"] 
        curMaxTotalSize = indexList[indexName]["curMaxTotalSize"]
        earliestTime = indexList[indexName]["earliestTime"]
        
        frozenTimePeriodInDays = int(indexList[indexName]['frozenTimePeriodInSecs'])/60/60/24
        maxTotalDataSizeMB = float(indexList[indexName]['maxTotalDataSizeMB'])
        avgLicenseUsagePerDay = indexList[indexName]['avgLicenseUsagePerDay']

        #Company specific field here, the commented size per day in the indexes.conf file
        configuredSizePerDay = -1
        if (indexList[indexName].has_key("sizePerDayInMB")):
            configuredSizePerDay = int(indexList[indexName]["sizePerDayInMB"])

        oversized = False
        calcSize = False
        estimatedDaysForCurrentSize = 0
        #calculate the index size usage based on recent license usage data and divide by the number of indexers we have
        calcSize = (storageRatio * avgLicenseUsagePerDay * frozenTimePeriodInDays)/numberOfIndexers
        
        #This index has data but no incoming data during the measurement period (via the license logs), capping the index at current size + contingency
        #or the estimated size if we have it
        if (calcSize == 0.0):
            #We have zero incoming data so we should be fine to store the frozen time period in days amount of data
            estimatedDaysForCurrentSize = frozenTimePeriodInDays

            #If this indexed was never sized through the sizing script *and* it has no recent data
            #then we cap it at current size + contingency rather than just leaving it on defaults
            #If it had a configured size it's dealt with later in the code
            if (configuredSizePerDay < 0):
                #ensure that our new sizing does not drop below what we already have on disk
                largestOnDiskSize = float(indexList[indexName]["curMaxTotalSize"])
                #add the contingency calculation in as we don't want to size to the point where we drop data once we apply on any of the indexers
                largestOnDiskSize = int(round(largestOnDiskSize * sizingContingency))
                #This now becomes our tuned size
                #However we cannot go below the lower index size limit restriction...
                logger.info("Index: %s has 0 incoming data for time period, capping size per indexer at %s or %s (whichever is greater)" % (indexName, largestOnDiskSize, lowerIndexSizeLimit))
                
                if (largestOnDiskSize < lowerIndexSizeLimit):
                    largestOnDiskSize = lowerIndexSizeLimit

                #Store the calculated value for later use
                indexList[indexName]['calcMaxTotalDataSizeMB'] = largestOnDiskSize
        #The index had recent data so we can estimate approx number of days we have left 
        #calcSize is the amount of data we will use per indexer
        else:
            if storageRatio == 0.0:
                estimatedDaysForCurrentSize = frozenTimePeriodInDays
            else:
                estimatedDaysForCurrentSize = int(round(maxTotalDataSizeMB / ((storageRatio * avgLicenseUsagePerDay * undersizingContingency)/numberOfIndexers)))
            
        #We leave a bit of room spare just in case by adding a contingency sizing here
        calcSize = int(round(calcSize*sizingContingency))

        #Store the estimate of how much we will likely use based on sizing calculations, note that we later increase the calcSize
        #to be higher than the minimum limit so we need to store this separately
        indexList[indexName]['estimatedTotalDataSizeMB'] = int(round(calcSize))
        
        #Bucket explosion occurs if we undersize an index too much so cap at the lower size limit
        if (calcSize < lowerIndexSizeLimit):
            calcSize = lowerIndexSizeLimit

        #Add our calculated size back in for later, int just in case the lowerIndexSizeLimit is a float
        indexList[indexName]['calcMaxTotalDataSizeMB'] = int(round(calcSize))
        
        licenseDataFirstSeen = indexList[indexName]["firstSeen"]
        
        #This flag is set if the index is undersized on purpose (i.e. we have a setting that says to set it below the limit where we lose data
        donotIncrease = False
        
        #This index was never sized so auto-size it
        if (configuredSizePerDay < 0):
            if (not calcSize):
                #Assume we're ok as we have no data to say otherwise...this could be for example a new index about to receive data
                #so do nothing here
                oversized = False 
            elif (licenseDataFirstSeen < minimumDaysOfLicenseForSizing):
                oversized = False
            else:
                #If we have oversized the index by a significant margin we do something, if not we take no action
                percEst = calcSize / maxTotalDataSizeMB
                logger.debug("perc est for calcSize/maxTotalSizeMB is %s " % (percEst))
                if (percEst < percBeforeAdjustment):
                    #Index is oversized > ?% therefore we can use our new sizing to deal with this
                    logger.info("Index: %s is oversized, perc %s or calculatedSize/maxTotalSize (%s/%s) is less than the adjustment threshold of %s, index will be resized, earliest data is %s" % (indexName, percEst, calcSize,  maxTotalDataSizeMB, percBeforeAdjustment, earliestTime))
                    oversized = True
        else:
            if (not calcSize):
                #Assume we're ok as we have no data to say otherwise...this could be for example a new index about to receive data
                #so do nothing here
                oversized = False
            #In this scenario we have some license data but not enough to determine the index is oversized, so assume false as this isn't a risk...
            elif (licenseDataFirstSeen < minimumDaysOfLicenseForSizing):
                oversized = False
            else:
                #size estimate on disk based on the compression ratio we have seen, and the the estimated size in the config file            
                sizeEst = (configuredSizePerDay * storageRatio * frozenTimePeriodInDays)/numberOfIndexers
                #including contingency
                sizeEst = sizeEst * sizingContingency
                            
                #3000MB is the lower bound as 3*auto sized buckets + a little extra is 3000MB
                if (sizeEst < lowerIndexSizeLimit):
                    sizeEst = lowerIndexSizeLimit
                
                usageBasedCalculatedSize = indexList[indexName]['calcMaxTotalDataSizeMB']
                #If the sizing was previously decided during a sizing discussion, then allocate the requested size
                indexList[indexName]['calcMaxTotalDataSizeMB'] = int(round(sizeEst))
                newCalcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']
                logger.debug("Index: %s, based on previous calculations the maxTotalDataSizeMB required is %sMB, however sizing comment is %sMB/day so re-calculated maxTotalDataSizeMB as %sMB, oldest data found is %s days old" % (indexName, usageBasedCalculatedSize, configuredSizePerDay, indexList[indexName]['calcMaxTotalDataSizeMB'], earliestTime))

                #Skip the zero size estimated where storageRatio == 0.0 or license usage is zero
                if (storageRatio != 0.0 and avgLicenseUsagePerDay != 0):
                    estimatedDaysForCurrentSize = int(round(newCalcMaxTotalDataSizeMB / ((storageRatio * avgLicenseUsagePerDay * undersizingContingency)/numberOfIndexers)))
                
                #If the commented size would result in data loss or an undersized index and we have a comment about this it's ok to keep it undersized
                if (usageBasedCalculatedSize > newCalcMaxTotalDataSizeMB):
                    if doNotLoseData:
                        indexList[indexName]['calcMaxTotalDataSizeMB'] = usageBasedCalculatedSize
                        logger.info("Index: %s has more data than expected and has a comment-based sizing estimate of %s however average is %s, current size would fit %s days, frozenTimeInDays %s, increasing the size of this index, oldest data found is %s days old" % (indexName, configuredSizePerDay, avgLicenseUsagePerDay, estimatedDaysForCurrentSize, frozenTimePeriodInDays, earliestTime))
                    else:
                        logger.warn("Index: %s has more data than expected and has a comment-based sizing estimate of %s however average is %s, data loss is likely after %s days, frozenTimeInDays %s, oldest data found is %s days old" % (indexName, configuredSizePerDay, avgLicenseUsagePerDay, estimatedDaysForCurrentSize, frozenTimePeriodInDays, earliestTime))
                        donotIncrease = True
                #If the newly calculated size has increased compared to previous size multiplied by the undersizing contingency
                elif (estimatedDaysForCurrentSize < frozenTimePeriodInDays):
                    #We will increase the sizing for this index
                    logger.info("Index: %s requires more sizing currently %s new sizing %s, commentedSizePerDay %s, frozenTimeInDays %s, average usage per day %s, oldest data found is %s days old" % (indexName, maxTotalDataSizeMB, newCalcMaxTotalDataSizeMB, configuredSizePerDay, frozenTimePeriodInDays, avgLicenseUsagePerDay, earliestTime))

                #At some point this index was manually sized to be bigger than the comment, fix it now, this may drop it below the expected frozen time period in days
                if (maxTotalDataSizeMB > newCalcMaxTotalDataSizeMB):
                    #Determine the % difference between the new estimate and the previous maxTotalDataSizeMB= setting
                    percEst = newCalcMaxTotalDataSizeMB / maxTotalDataSizeMB
                    logger.debug("perc est %s based on %s %s %s %s and max total %s" % (percEst, configuredSizePerDay, storageRatio, frozenTimePeriodInDays, numberOfIndexers, maxTotalDataSizeMB))
                    #If we are below the threshold where we adjust take action
                    if (percEst < percBeforeAdjustment):
                        oversized = True
                        donotIncrease = True
                        
                        #We warn if we drop below the frozen time period in seconds and do not warn if we are staying above it
                        if (estimatedDaysForCurrentSize < frozenTimePeriodInDays):
                            logger.warn("Index: %s comment-based sizing estimate of %s, %s comp ratio perc, current size of %s, new size estimate of %s, frozenTimeInDays %s, estimated days %s, average usage per day %s, this index will be decreased in size, oldest data found is %s days old" % (indexName, configuredSizePerDay, storageRatio, maxTotalDataSizeMB, newCalcMaxTotalDataSizeMB, frozenTimePeriodInDays, estimatedDaysForCurrentSize, avgLicenseUsagePerDay, earliestTime))
                        else:
                            logger.info("Index: %s comment-based sizing estimate of %s, %s comp ratio perc, current size of %s, new size estimate of %s, frozenTimeInDays %s, estimated days %s, average usage per day %s, this index will be decreased in size, oldest data found is %s days old" % (indexName, configuredSizePerDay, storageRatio, maxTotalDataSizeMB, newCalcMaxTotalDataSizeMB, frozenTimePeriodInDays, estimatedDaysForCurrentSize, avgLicenseUsagePerDay, earliestTime))
          
        requiresChange = False
        if indexName in indexesRequiringChanges:
            requiresChange = indexesRequiringChanges[indexName]
            logger.debug("indexName: %s requires change set to %s" % (indexName, requiresChange))
        
        #If the estimates show we cannot store the expected frozenTimePeriodInDays on disk we need to take action
        #this is an undersized scenario and we need a larger size unless of course we have already capped the index at this size and data loss is expected
        if (estimatedDaysForCurrentSize < frozenTimePeriodInDays and not donotIncrease):
            #TODO if (configuredSizePerDay != "N/A") do we still increase an undersized index or let it get frozen anyway because the disk estimates were invalid?!
            #also the above would be assuming that the disk estimates were based on the indexers we have now configured
            #for now assuming we always want to increase and prevent data loss...
            
            #If we appear to be undersized but we don't have enough data yet
            if (licenseDataFirstSeen < minimumDaysOfLicenseForSizing):
                logger.info("Index: %s appears to be undersized but has %s days of license data, minimum days for sizing is %s, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, earliestTime is %s" % (indexName, licenseDataFirstSeen, minimumDaysOfLicenseForSizing, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, earliestTime))
            #If we have enough data and we're doing a bucket size adjustment we are now also doing the index sizing adjustment
            elif (requiresChange != False and oversized != True):
                requiresChange = requiresChange + "_sizing"
                #Write comments into the output files so we know what tuning occured and when
                indexList[indexName]['changeComment']['sizing'] = "# maxTotalDataSizeMB previously %s, auto-tuned on %s\n" % (indexList[indexName]['maxTotalDataSizeMB'], todaysDate)
            #We need to do an index sizing adjustment
            else:
                #If we previously said the index was oversized, but it is undersized, this can only happen when the comment advising the size is exceeded by the size of the data
                #depending on policy we can either not re-size this *or* we can just re-size it to fix the data we received, for now using re-sizing based on what we have recieved
                #rather than the original estimate
                if (oversized == True):
                    logger.info("Index: %s undersized but has %s days of license data *but* index sizing comment advises index oversized, increasing size, minimum days for sizing is %s, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, earliestTime is %s" % (indexName, licenseDataFirstSeen, minimumDaysOfLicenseForSizing, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, earliestTime))
                    oversized = False
                    
                requiresChange = "sizing"
                if (not indexList[indexName].has_key('changeComment')):
                    indexList[indexName]['changeComment'] = {}
                
                #Write comments into the output files so we know what tuning occured and when
                str = "# maxTotalDataSizeMB previously %s, had room for %s days, auto-tuned on %s\n" % (indexList[indexName]['maxTotalDataSizeMB'], estimatedDaysForCurrentSize, todaysDate)
                indexList[indexName]['changeComment']['sizing'] = str

            #Record this in our tuning log
            logger.info("Index: %s undersized, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, license data first seen %s days ago, earliestTime is %s" % (indexName, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, licenseDataFirstSeen, earliestTime))

        #Is this index oversized in our settings
        if (oversized == True):
            #ensure that our new sizing does not drop below what we already have on disk
            #as this would cause data to freeze on at least 1 indexer
            largestOnDiskSize = float(indexList[indexName]["curMaxTotalSize"])
            largestOnDiskSize = int(round(largestOnDiskSize * sizingContingency))

            maxTotalDataSize = indexList[indexName]['maxTotalDataSizeMB']

            str = "# maxTotalDataSizeMB previously %s, auto-tuned on %s\n" % (maxTotalDataSize, todaysDate)

            calcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']

            #Sanity check to ensure our tuned size doesn't drop below our largest on-disk size
            if (calcMaxTotalDataSizeMB < largestOnDiskSize):
                logger.warn("Index: %s oversized, calculated size %s less than max on-disk size %s, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, earliestTime is %s" % (indexName, calcMaxTotalDataSizeMB, largestOnDiskSize, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, earliestTime))
                logger.warn("Index: %s oversized, refusing to trigger immediate data loss and changing back to largestOnDiskSize %s from %s" % (indexName, largestOnDiskSize, calcMaxTotalDataSizeMB))
                
                indexList[indexName]['calcMaxTotalDataSizeMB'] = largestOnDiskSize
                indexList[indexName]['estimatedTotalDataSizeMB'] = largestOnDiskSize

            #Write comments into the output files so we know what tuning occured and when
            if (not indexList[indexName].has_key('changeComment')):
                indexList[indexName]['changeComment'] = {}
            
            #If we have skip problem indexes on *and* we are reducing an index in size that would result in a cap at current disk usage levels
            #then we skip it as it might lose data after this change
            if skipProblemIndexes and calcMaxTotalDataSizeMB < largestOnDiskSize:
                logger.info("Index: %s oversized but skipProblemIndexes flag is on, therefore sparing this index for sizing based adjustments" % (indexName))
            else:
                indexList[indexName]['changeComment']['sizing'] = str
                #If we are already changing the bucket sizing we are now also changing the index sizing
                if (requiresChange != False):
                    requiresChange = requiresChange + "_sizing"
                #We are changing the index sizing only
                else:
                    requiresChange = "sizing"
            
            #Record this in our log
            logger.info("Index: %s oversized, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, license data first seen %s days ago, earliestTime is %s" % (indexName, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, licenseDataFirstSeen, earliestTime))

        #If we haven't yet added a sizing comment and we need one, this only happens during the initial runs
        #after this all indexes should have comments and this shouldn't run
        if (configuredSizePerDay < 0):
            if (requiresChange != False):
                requiresChange = requiresChange + "_sizingcomment"
            else:
                requiresChange = "sizingcomment"
            
            if (not indexList[indexName].has_key('changeComment')):
                indexList[indexName]['changeComment'] = {}
                
            #Write comments into the output files so we know what tuning occured and when           
            str = "# auto-size comment created on %s (%s days @ %sMB/day @ %s compression ratio)\n" % (todaysDate, frozenTimePeriodInDays, avgLicenseUsagePerDay, storageRatio)
            
            indexList[indexName]['changeComment']['sizingcomment'] = str
            logger.warn("Index: %s, added %s" % (indexName, str))
        
        #If this index requires change we record this for later
        if (requiresChange != False):
            indexesRequiringChanges[indexName] = requiresChange 
            logger.debug("Index: %s, requires change status of %s" % (indexName, requiresChange))
            
            #An additional scenario occurs that we have calculated the maxTotalDataSizeMB but we have also custom set the coldPath.maxDataSizeMB
            #and homePath.maxDataSizeMB and therefore must calculate them now...(as they will be invalid after we re-size the index)
            homePathMaxDataSizeMB = indexList[indexName]['homePathMaxDataSizeMB']
            curHomePathMaxDataSizeMB = indexList[indexName]['homePathMaxDataSizeMB']
            curColdPathMaxDataSizeMB = indexList[indexName]['coldPathMaxDataSizeMB']
           
            skipConfFile = False
            #if we're only changing the sizing comment then don't attempt sizing the maxDataSizeMB
            if (requiresChange == "sizingcomment"):
                pass
            #We custom specified our homePathDataSize, therefore assume we need to calculate both values here
            elif (homePathMaxDataSizeMB != 0.0):
                percMultiplier = homePathMaxDataSizeMB / maxTotalDataSizeMB
                
                #Assume that this % is the amount of data we should allocate to the homePath.maxDataSizeMB
                calcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']
                homePathMaxDataSizeMB = int(round(calcMaxTotalDataSizeMB * percMultiplier))
                coldPathMaxDataSizeMB = calcMaxTotalDataSizeMB - homePathMaxDataSizeMB
                
                if (homePathMaxDataSizeMB < lowerIndexSizeLimit):
                    sizeAdded = lowerIndexSizeLimit - homePathMaxDataSizeMB
                    coldPathMaxDataSizeMB = coldPathMaxDataSizeMB - sizeAdded
                    homePathMaxDataSizeMB = lowerIndexSizeLimit
                    
                    #We don't want this number too small either, at least 1 bucket should fit here
                    if (coldPathMaxDataSizeMB < smallBucketSize):
                        coldPathMaxDataSizeMB = smallBucketSize
                    
                    indexList[indexName]['calcMaxTotalDataSizeMB'] = homePathMaxDataSizeMB + coldPathMaxDataSizeMB
                    
                #Add the newly calculated numbers back in for later use in the indexes.conf output
                indexList[indexName]['homePathMaxDataSizeMB'] = homePathMaxDataSizeMB
                indexList[indexName]['coldPathMaxDataSizeMB'] = coldPathMaxDataSizeMB
                
                if int(maxTotalDataSizeMB) == int(indexList[indexName]['calcMaxTotalDataSizeMB']) and int(homePathMaxDataSizeMB) == int(curHomePathMaxDataSizeMB) and int(curColdPathMaxDataSizeMB) == int(coldPathMaxDataSizeMB):
                    logger.debug("This would make no difference to index sizing after correctly sizing home/cold paths")
                    if indexesRequiringChanges[indexName] == "sizing":
                        del indexesRequiringChanges[indexName]
                        skipConfFile = True
                    elif indexesRequiringChanges[indexName] == "bucket_sizing":
                        indexesRequiringChanges[indexName] = "bucket"
                    else:
                        logger.warn("Unhandled edge case here, contining on, indexes requiring change value is %s" % (indexesRequiringChanges[indexName]))
            
            #Add the conf file to the list we need to work on
            if (not confFile in confFilesRequiringChanges and not skipConfFile):
                confFilesRequiringChanges.append(confFile)
                logger.debug("indexName: %s, conf file %s now requires changes" % (indexName, confFile))
     
        #The debugging statement just in case something goes wrong and we have to determine why (the everything statement)
        logger.debug("Index %s, frozenTimePeriodInDays %s, maxTotalDataSizeMB %s , recentLicensePerDayInMB %s, EstimatedSizeFromComment %s, CompMultiplier %s, CalculatedSizeRequiredPerIndexer %s, EstimatedDaysForCurrentSizing %s, calculated size usage (after overrides) %s, license data first seen %s, oldest data found is %s days old" % (indexName, frozenTimePeriodInDays, maxTotalDataSizeMB, avgLicenseUsagePerDay, configuredSizePerDay, storageRatio, calcSize, estimatedDaysForCurrentSize, indexList[indexName]['calcMaxTotalDataSizeMB'], licenseDataFirstSeen, earliestTime))

        if indexList[indexName]['calcMaxTotalDataSizeMB'] == False:
            calcSize = 0
        else:
            calcSize = int(indexList[indexName]['calcMaxTotalDataSizeMB'])
        calcSizeTotal = calcSizeTotal + calcSize
        
        #If we have run the required checks on the index mark it True, otherwise do not, this is used later and relates to the limited index runs
        indexList[indexName]["checked"] = True
    
    return confFilesRequiringChanges, indexesRequiringChanges, calcSizeTotal

#We may or may not want to run the entire complex tuning script
if args.bucketTuning or args.indexSizing:
    indexTuningPresteps(utility, indexList, args.indexIgnoreList, args.earliestLicense, args.latestLicense, args.indexNameRestriction, args.indexLimit, args.indexerhostnamefilter, args.useIntrospectionData)
    
    indexesRequiringChanges = {}
    confFilesRequiringChanges = []
    
    if args.bucketTuning:
        (indexesRequiringChanges, confFilesRequiringChanges) = runBucketSizing(utility, indexList, args.indexNameRestriction, args.indexLimit, args.numHoursPerBucket, args.bucketContingency, args.upperCompRatioLevel, args.minSizeToCalculate, args.numberOfIndexers)

    if args.indexSizing:
        (confFilesRequiringChanges, indexesRequiringChanges, calcSizeTotal) = runIndexSizing(utility, indexList, args.indexNameRestriction, args.indexLimit, args.numberOfIndexers, args.lowerIndexSizeLimit, args.sizingContingency, args.minimumDaysOfLicenseForSizing, args.percBeforeAdjustment, args.doNotLoseData, args.undersizingContingency, args.smallBucketSize, args.skipProblemIndexes, indexesRequiringChanges, confFilesRequiringChanges)

    if args.outputTempFilesWithTuning:
        indextuning_indextempoutput.outputIndexFilesIntoTemp(logging, confFilesRequiringChanges, indexList, args.workingPath, indexesRequiringChanges)
    
    if args.workWithGit:
        success = True
        if not args.gitRepoURL:
            logger.warn("git repo URL not supplied, not performing any git work")
            success = False
        
        if success:
            if os.path.isdir(args.gitWorkingDir):
                logger.debug("Git directory exists, determine if git is working")
                (output, stderr, res) = utility.runOSProcess("cd " + args.gitWorkingDir + args.gitRoot + "; git status")
                
                if res == False:
                    logger.info("Failure!")
                    #Something failed? Easiest solution is to wipe the directory + allow re-clone to occur or similar
                    #if that fails we cancel the git component completely perhaps?!
                    logger.warn("git error occurred while attempting to work with directory {} stdout '{}' stderr '{}'".format(args.gitWorkingDir, output, stderr))
                    shutil.rmtree(args.gitWorkingDir)
                    os.makedirs(args.gitWorkingDir)
                    logger.debug("Attempting clone again from {}".format(args.gitRepoURL)) 
                    
                    if args.gitFirstConnection:
                        #This is a once off make it a switch?!
                        (output, stderr, res) = utility.runOSProcess("ssh -n -o \"BatchMode yes\" -o StrictHostKeyChecking=no " + args.gitRepoURL[:args.gitRepoURL.find(":")])
                        if res == False:
                            logger.warn("Unexpected failure while attempting to trust the remote git repo. stdout '%s', stderr '%s'" % (output, stderr))
                    
                    (output, stderr, res) = utility.runOSProcess("cd %s; git clone %s" % (args.gitWorkingDir, args.gitRepoURL))
                    if res == False:
                        logger.warn("git clone failed for some reason...on url %s stdout '%s', stderr '%s'" % (args.gitRepoURL, output, stderr))
                else:
                    logger.debug("result from git command: %s" % (res))
                    logger.info("Success")
            else:
                if not os.path.isdir(args.gitWorkingDir):
                    os.makedirs(args.gitWorkingDir)
                (output, stderr, res) = utility.runOSProcess("cd %s; git clone %s" % (args.gitWorkingDir, args.gitRepoURL))
                if res == False:
                    logger.warn("git clone failed for some reason...on url %s, output is '%s', stderr is '%s'" % (args.gitRepoURL, output, stderr))
            
            gitPath = args.gitWorkingDir + args.gitRoot
            
            #Always start from master and the current version
            (output, stderr, res) = utility.runOSProcess("cd %s; git checkout master; git pull" % (gitPath))
            if res == False:
                logger.warn("git checkout master or git pull failed, output is '%s' stderr is '%s'" % (output, stderr))
            
            #At this point we've written out the potential updates
            indextuning_indextempoutput.outputIndexFilesIntoTemp(logging, confFilesRequiringChanges, indexList, gitPath, indexesRequiringChanges, replaceSlashes=False)
            (output, stderr, res) = utility.runOSProcess("cd %s; git status | grep \"nothing to commit\"" % (gitPath))
            if res == False:
                #We have one or more files to commit, do something
                #Then we git checkout -b indextuning_20181220_1120
                todaysDate = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
                (output, stderr, res) = utility.runOSProcess("cd {0}; git checkout -b {1}_{2} 2>&1; git commit -am \"Updated by index auto-tuning algorithm on {2}\" 2>&1; git push origin {1}_{2} 2>&1".format(gitPath, args.gitBranch, todaysDate))
                if res == False:
                    logger.warn("Failure while creating new branch and pushing to remote git repo. stdout '%s' stderr '%s'" % (output, stderr))
                else:
                    if args.gitLabToken and args.gitLabURL:
                        res = requests.post(args.gitLabURL, headers = { 'Private-Token': args.gitLabToken }, data={ 'target_branch' : 'master', 'source_branch' : args.gitBranch + "_" + todaysDate, 'title' : 'Automated merge request from index tuning script on ' + todaysDate }, verify=False)
                        if (res.status_code != requests.codes.ok and res.status_code != 201):
                            logger.error("URL=%s statuscode=%s reason=%s response=\"%s\"" % (args.gitLabURL, res.status_code, res.reason, res.text))
                        else:
                            logger.debug("Response is %s" % (res.text))
                    elif args.gitLabToken:
                        logger.warn("gitLabToken supplied but the gitLabURL has not been provided not creating merge request")
            else:
                logger.info("No changes to be checked into git")
            
#If we asked for sizing estimates only, and we're not running the dead index check only option
if args.sizingEstimates:
    totalIndexAllocation = 0
    totalEstimatedIndexAllocation = 0
    for index in indexList.keys():
        dataSizeMB = indexList[index]["maxTotalDataSizeMB"]
        totalIndexAllocation = totalIndexAllocation + dataSizeMB
        logger.debug("Index %s maxTotalDataSizeMB %s" % (index, dataSizeMB))
        if 'estimatedTotalDataSizeMB' in indexList[index]:
            estimatedTotalSize = indexList[index]['estimatedTotalDataSizeMB']
            logger.debug("Index %s estimatedTotalDataSizeMB %s" % (index, estimatedTotalSize))
            totalEstimatedIndexAllocation = totalEstimatedIndexAllocation + estimatedTotalSize
    totalVolSize = 0
    for vol in volList.keys():
        volSize = volList[vol]["maxVolumeDataSizeMB"]
        if vol != "_splunk_summaries":
            totalVolSize = totalVolSize + volSize
        logger.info("Vol %s maxVolumeDataSizeMB %s" % (vol, volSize))
    logger.info("Summary: total index allocated %s total vol (excluding _splunk_summaries) %s total" % (totalIndexAllocation, totalVolSize))
    
    if totalEstimatedIndexAllocation > 0:
        if args.indexLimit < len(indexList):
            logger.warn("Estimated size cannot be accurate as we have looked at %s of %s indexes" % (args.indexLimit, len(indexList)))
        logger.info("Estimated index usage: %s" % (totalEstimatedIndexAllocation))
    
if indexDirCheckRes:
    hotDirsChecked = indexDirCheckRes["hotDirsChecked"].keys()
    coldDirsChecked = indexDirCheckRes["coldDirsChecked"].keys()
    summariesDirsChecked = indexDirCheckRes["summariesDirsChecked"].keys()
    deadHotDirs = indexDirCheckRes["hotDirsDead"]
    deadColdDirs = indexDirCheckRes["coldDirsDead"]
    summariesDirsDead = indexDirCheckRes["summariesDirsDead"]

    #Note that duplicate values can be returned if the same path is used for hot/cold or summaries, for example /opt/splunk/var/lib/splunk
    #may be duplicated...
    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the 'hot' path: %s" % (hotDirsChecked))
    if (len(deadHotDirs.keys()) > 0):
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in deadHotDirs.keys():
            for entry in deadHotDirs[line]:
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    logger.info("Wiping directory %s" % (thedir))
                    shutil.rmtree(thedir)
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead hot dirs found")
        
    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the 'cold' path: %s" % (coldDirsChecked))
    if (len(deadColdDirs.keys()) > 0):
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in deadColdDirs.keys():
            for entry in deadColdDirs[line]:
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    logger.info("Wiping directory %s" % (thedir))
                    shutil.rmtree(thedir)
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead cold dirs found")

    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the 'summaries' path: %s" % (summariesDirsChecked))
    if (len(summariesDirsDead.keys()) > 0):
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in summariesDirsDead.keys():
            for entry in summariesDirsDead[line]:
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    logger.info("Wiping directory %s" % (thedir))
                    shutil.rmtree(thedir)
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead summary dirs found")

    if (len(deadHotDirs.keys()) > 0 and not args.deadIndexDelete):
        for line in deadHotDirs.keys():
            for entry in deadHotDirs[line]:
                print entry + "/" + line

    if (len(deadColdDirs.keys()) > 0 and not args.deadIndexDelete):
        for line in deadColdDirs.keys():
            for entry in deadColdDirs[line]:
                print entry + "/" + line

    if (len(summariesDirsDead.keys()) > 0 and not args.deadIndexDelete):
        for line in summariesDirsDead.keys():
            for entry in summariesDirsDead[line]:
                print entry + "/" + line

logger.info("End index sizing script" % (cleanArgs))