from __future__ import print_function
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
from requests.auth import HTTPBasicAuth
from index_bucket_sizing import run_bucket_sizing
from index_tuning_presteps import index_tuning_presteps
from indextuning_index_tuning import run_index_sizing

"""
What does this script do?

 Attempts to use the output of the command:
 splunk btool indexes list --debug
 To determine all indexes configured on this Splunk indexer and then provides tuning based on the previously seen license usage & bucket sizing
 in addition this script provides a list of directories which are located in the same directory as hot, cold or tstatsHomePath and if there is no matching
 index configuration suggests deletion via an output file

 The script takes no input, the script has multiple output options:
 * output example index file that can be diffed
 * create a git-based change
 * create a git-based change and create a merge request in gitlab

 In more depth:
 Index bucket sizing (max_data_size setting)
   * Based on output of dbinspect command determining how many hours worth of data will fit into a bucket
   * Based on target number of hours determines if the bucket sizing is currently appropriate
   * Based on recent license usage, compression ratio, number of indexers/buckets approx if the license usage seems reasonable or not as a sanity check
   *   the above sanity check can cause issues *if* a very small amount of data exists in the index, resulting in compression ratios of 500:1 (500 times larger on disk)
   *   min_size_to_calculate exists to prevent this from happening
   *   upper_comp_ratio_level provides an additional safety here to cap the maximum found compression ratio
   * If bucket size is auto_high_volume, and tuned size*contingency is less than 750MB, then auto will be suggested
   * If bucket size is auto tuned, auto_high_volume will be suggested if tuned size * contingency is larger than 750MB
   * If bucket was using a size-based max_data_size then the new value will be suggested (not recommended, auto/auto_high_volume are usually fine)

 Index sizing (max_total_data_size_mb)
   * Based on the last X days of license usage (configurable) and the storage compression ratio based on the most recent introspection data
     and multiplied by the number of days before frozen, multiplied by contingency, rep factor multiplier and divided by number of indexers to determine value per indexer
   * If the index has recent license usage *but* it does not match the minimum number of days we require for sizing purposes we do not size the index
     this covers, for example an index that has 2 days of license usage and it's too early to correctly re-size it
   * If the index has no data on the filesystem no tuning is done but this is recorded in the log (either a new index or an unused index)
   * If the index has zero recent license usage over the measuring period *and* it does not have a sizing comment then we cap it at the
     current maximum size * contingency with a lower limit of lower_index_size_limit, this parameter prevents bucket explosion from undersizing indexes
   * If the index has zero recent license usage over the measuring period *and* it does have a sizing comment then we do not change it
   * If the index has recent license usage and it is oversized, in excess of the perc_before_adjustment then we adjust (oversized index)
   * If the index has recent license usage and it is undersized (including a % contingency we add), we adjust (undersized index) currently we do this even if sizing comments exist
     note that this particular scenario can result in the index becoming larger than the original sizing comment, it is assumed that data loss should be avoided by the script
   * Finally if oversized we sanity check that we are not dropping below the largest on-disk size * contingency on any indexer as that would result in deletion of data

 sizingEstimates mode
    * Checking max_total_data_size_mb of each index, and the size of the volumes set in Splunk, determine if we have over-allocated based
      on the worst case usage scenario that all max_total_data_size_mb is in use
    * If index tuning is occurring and this switch is passed a more accurate estimate of index sizing is provided

 deadIndexCheck mode
    * Check the relevant Splunk directories for index storage and determine if there are directories in these locations that no longer map to an index stanza in the config
    this commonly happens when an index is renamed leaving extra directories on the filesystem that will never delete themselves
    * Unless the deadIndexDelete flag is also passed in this will not actually delete any files, it will provide a listing of what appears to be extra files

 Indexes datatype can be event or metric, if metrics we do the same tuning but we avoid data loss as the chance of accidentally flooding a metrics index with data
 is very low...dbinspect still works along with other queries, and as of 7.3+ the license usage is now limited to 150 bytes/metric (it can be less now)
"""

#In addition to console output we write the important info into a log file, console output for debugging purposes only
output_log_file = "/tmp/indextuning.log"

app_hosting_exclusion_list = 'monitoring'

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
              'filename' : output_log_file,
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

# Create the argument parser
parser = argparse.ArgumentParser(description="Re-tune the indexes.conf files of the current indexer based on " \
    " passed in tuning parameters and an estimation of what is required")

# Useful for testing only, should be set to unlimited by default, limit setting
parser.add_argument('-indexLimit', help='Number of indexes to run through before tuning is stopped at that point in time', default=9999, type=int)
parser.add_argument('-destURL', help='URL of the REST/API port of the Splunk instance, https://localhost:8089/ for example', default="localhost:8089")

# How far to go back in queries related to bucket information
parser.add_argument('-oldest_data_found', help='Earliest point in time to find bucket information', default="-16d")

# Earliest time is 14 days ago and we start at midnight to ensure we get whole days worth of license usage
parser.add_argument('-earliest_license', help='Earliest point in time to find license details (use @d to ensure this starts at midnight)', default="-30d@d")
parser.add_argument('-latest_license', help='Latest point in time to find license details (use @d to ensure this ends at midnight)', default="@d")
parser.add_argument('-numberOfIndexers', help='Number of indexers the tuning should be based on', default="6", type=int)

# For bucket tuning, aim for 24 hours of data per bucket for now, we add contingency to this anyway
parser.add_argument('-num_hours_per_bucket', help='Aim for approximate number of hours per bucket (not including contingency)', default="24")

# Add 20% contingency to the result for buckets
parser.add_argument('-bucket_contingency', help='Contingency multiplier for buckets', default=1.2, type=float)

# Add 25% contingency to the result for index sizing purposes
parser.add_argument('-sizing_continency', help='Contingency multiplier for index sizing', default=1.25, type=float)

# What % do we leave spare in our indexes before we increase the size? 20% for now
# Note an additional safety that checks the max on disk size per index can also override this
# By having the sizing_continency larger than this setting we hope that re-sizing will rarely occur once increased correctly
# as it would have to change quite a bit from the average...
parser.add_argument('-undersizing_continency', help='Contingency multiplier before an index is increased (1.2 is if the index is undersized by less than 20 perc. do nothing)', default=1.2, type=float)

# What host= filter do we use to find index on-disk sizing information for the compression ratio?
parser.add_argument('-indexerhostnamefilter', help='Host names for the indexer servers for determining size on disk queries', default="pucq-sp*-*")

# What's the smallest index size we should go to, to avoid bucket explosion? At least max_hot_buckets*max_data_size + some room? Hardcoded for now
parser.add_argument('-lower_index_size_limit', help='Minimum index size limit to avoid bucket explosion', default=3000, type=int)

# What's the minimum MB we can leave for a bucket in cold for a really small index?
parser.add_argument('-smallbucket_size', help='Minimum cold section size for a small bucket', default=800, type=int)

# If the index is oversized by more than this % then adjustments will occur
# this prevents the script from making small adjustments to index sizing for no reason, it has to be more than 20% oversized
# if we utilise 0.8 as the perc_before_adjustment
# If undersized we always attempt to adjust immediately
parser.add_argument('-perc_before_adjustment', help='Multiplier for downsizing an oversized index (0.8 means that the index must be more than 20 perc. oversized for any adjustment to occur)', default=0.8, type=float)

# Path to store temporary files such as the indexes.conf outputs while this is running
parser.add_argument('-workingPath', help='Path used for temporary storage of index.conf output', default="/tmp/indextuningtemp")

# Number of days of license data required for sizing to occur, if we don't have this many days available do not attempt to size
parser.add_argument('-min_days_of_license_for_sizing', help="Number of days of license data required for sizing to occur, " \
    "if we don't have this many days available do not attempt to resize", default=15, type=int)

# How many MB of data should we have before we even attempt to calculate bucket sizing on a per indexer basis?
# where a number too small results in a giant compression ratio where stored size > raw data
parser.add_argument('-min_size_to_calculate', help="Minimum bucket size before calculation is attempted", default=100.0, type=float)

# Exclude default directories from /opt/splunk/var/lib/splunk and should not be deleted as they won't exist in the indexes.conf files
parser.add_argument('-excludedDirs', help="List of default directories to exclude from been listed in "\
    " the deletion section", default='kvstore,.snapshots,lost+found,authDb,hashDb,persistentstorage,fishbucket,main,$_index_name')

# If the compression ratio is above this level we throw a warning
parser.add_argument('-upper_comp_ratio_level', help="Comp ratio limit where a warning in thrown rather than calculation "\
    "done on the bucket sizing if this limit is exceeded", default=6.0, type=float)

# Multiply license usage by this multiplier to take into account index replication
parser.add_argument('-rep_factor_multiplier', help='Multiply by rep factor (for example if 2 copies of raw/indexed data, 2.0 works', default=2.0, type=float)

parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-username', help='Username to login to the remote Splunk instance with', required=True)
parser.add_argument('-password', help='Password to login to the remote Splunk instance with', required=True)
parser.add_argument('-indexNameRestriction', help='List of index names to run against (defaults to all indexes)')
parser.add_argument('-deadIndexCheck', help='Only use the utility to run the dead index check, no index tuning required', action='store_true')
parser.add_argument('-deadIndexDelete', help='After running the dead index check perform an rm -R on the directories "\
    "which appear to be no longer in use, use with caution', action='store_true')
parser.add_argument('-do_not_lose_data_flag', help='By default the index sizing estimate overrides any attempt to prevent data loss, "\
    "use this switch to provide extra storage to prevent data loss (free storage!)', action='store_true')
parser.add_argument('-sizingEstimates', help='Only run sizing estimates (do not resize indexes)', action='store_true')
parser.add_argument('-indexTuning', help='Run index tuning & bucket tuning (resize indexes + buckets)', action='store_true')
parser.add_argument('-bucketTuning', help='Only run bucket tuning (resize buckets only)', action='store_true')
parser.add_argument('-indexSizing', help='Only run index tuning (resize indexes only)', action='store_true')
parser.add_argument('-all', help='Run index sizing and sizing estimates, along with a list of unused indexes', action='store_true')
parser.add_argument('-useIntrospectionData', help='Use introspection data rather than the REST API data for index bucket calculations, "\
     "slightly faster but earliest time may not be 100 percent accurate (data is likely older than logged)', action='store_true')
parser.add_argument('-workWithGit', help='Check changes back into git', action='store_true')
parser.add_argument('-gitWorkingDir', help='Directory to perform git clone / pull / commits in', default="/tmp/indextuning_git_temp")
parser.add_argument('-gitFirstConnection', help='Run an SSH command to add the git repo to the list of trusted SSH fingerprints')
parser.add_argument('-gitRepoURL', help='URL of the git repository (using an SSH key)')
parser.add_argument('-gitBranch', help='git branch prefix to use to create to send to remote repo', default="indextuning")
parser.add_argument('-gitRoot', help='What is the directory under the git repository where the master-apps directory sits?', default="/master-apps")
parser.add_argument('-gitLabToken', help='The gitLab private token or access token, if supplied a merge request is created using this token')
parser.add_argument('-gitLabURL', help='URL of the remote gitlab server to work with', default="https://localhost/api/v4/projects/1/merge_requests")
parser.add_argument('-outputTempFilesWithTuning', help='Output files into the working path with tuning results (optional)', action='store_true')
parser.add_argument('-no_sizing_comments', help='Do not add auto-sizing comments on entries that do not have a commented size (optional)', action='store_true')

# Skip indexes where the size on disk is greater than the estimated size (i.e. those who have serious balance issues or are exceeding usage expectations)
parser.add_argument('-skipProblemIndexes', help="Skip re-sizing attempts on the index size for any index perceived as a problem "\
    "(for example using more disk than expected or similar)", action='store_true')

# helper function as per https://stackoverflow.com/questions/31433989/return-copy-of-dictionary-excluding-specified-keys
def without_keys(d, keys):
    return {x: d[x] for x in d if x not in keys}

"""
 Initial setup
"""

args = parser.parse_args()

# If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

args.excludedDirs = args.excludedDirs.split(",")

if args.all:
    args.sizingEstimates = True
    args.indexTuning = True
    args.deadIndexCheck = True

if args.indexTuning:
    args.bucketTuning = True
    args.indexSizing = True

# index_tuning_exclusion_list populating option
# | makeresults | eval index="_internal,_audit,_telemetry,_thefishbucket,_introspection,history,default,splunklogger,notable_summary,ioc,threat_activity,endpoint_summary,whois,notable,risk,cim_modactions,cim_summary,xtreme_contexts" | makemv delim="," index | mvexpand index | table index | outputlookup index_tuning_exclusion_list
if args.bucketTuning or args.indexSizing:
    index_ignore_url = 'https://' + args.destURL + '/servicesNS/nobody/' + app_hosting_exclusion_list + '/storage/collections/data/index_tuning_exclusion_list'
    logger.debug("Attempting to obtain index ignore list via rest call to url=%s" % (index_ignore_url))
    res = requests.get(index_ignore_url, verify=False, auth=HTTPBasicAuth(args.username, args.password))

    if res.status_code != requests.codes.ok:
        logger.fatal("Failure to obtain index_ignore_list list on url=%s in status_code=%s reason=%s response=\"%s\"" % (index_ignore_url, res.status_code, res.reason, res.text))
        sys.exit(-1)

    logger.debug("index ignore list response=\"%s\"" % (res.text))
    index_ignore_json = res.json()
    if len(index_ignore_json) != 0:
        index_ignore_list = [ item['index'] for item in index_ignore_json ]
        logger.info("index=%s added to ignore list for tuning purposes" % (index_ignore_list))
    else:
        logger.warn("Found no indexes to add to ignore list for tuning purposes, this normally indicates a problem that should be checked")

# Setup the utility class with the username, password and URL we have available
logger.debug("Creating utility object with username=%s, destURL=%s, oldest_data_found=%s" % (args.username, args.destURL, args.oldest_data_found))
utility = idx_utility.utility(args.username, args.password, args.destURL, args.oldest_data_found)

# Determine the index/volume list we are working with
index_list, vol_list = utility.parse_btool_output()

# We only care about unique dirs to check and unique directories that are unused
dead_index_dir_list = {}
dead_summary_dir_list = {}

# Cleanup previous runs
if os.path.isdir(args.workingPath):
    logger.debug("Deleting old dir=%s after previous run" % (args.workingPath))
    shutil.rmtree(args.workingPath)

excludedList = [ "password", "gitLabToken" ]
clean_args = without_keys(vars(args), excludedList)
logger.info("Index sizing script running with args=\"%s\"" % (clean_args))

"""
 Check for directories on the filesystem that do not appear to be related to any index

 Determine locations that exist on the filesystem, and if they don't exist in the list from the btool output
 they are likely dead index directories from indexes that were once here but now removed
 TODO this could be a function and moved outside the main code
 Note this is here because later we modify the index_list to remove ignored indexes
 where now we want them in the list to ensure we do not attempt to delete in use directories!

 the checkdirs function returns the top level directories from the indexes.conf which we should be checking, for example /opt/splunk/var/lib/splunk
 Should not be passing around objects but this will do for now
"""
index_dir_check_res = False
if args.deadIndexCheck:
    index_dir_check_res = indextuning_dirchecker.check_for_dead_dirs(index_list, vol_list, args.excludedDirs, utility)

# We need to ignore these indexes for re-sizing purposes
# but we need the details of these indexes for sizing estimates done later...
indexes_not_getting_sized = {}

"""
 Begin main logic section
"""
# We may or may not want to run the entire complex tuning script
if args.bucketTuning or args.indexSizing:
    index_tuning_presteps(utility, index_list, index_ignore_list, args.earliest_license, args.latest_license, args.indexNameRestriction, args.indexLimit, args.indexerhostnamefilter, args.useIntrospectionData, indexes_not_getting_sized)

    indexes_requiring_changes = {}
    conf_files_requiring_changes = []

    if args.bucketTuning:
        (indexes_requiring_changes, conf_files_requiring_changes) = run_bucket_sizing(utility, index_list, args.indexNameRestriction, args.indexLimit, args.num_hours_per_bucket,
        args.bucket_contingency, args.upper_comp_ratio_level, args.min_size_to_calculate, args.numberOfIndexers, args.rep_factor_multiplier, args.do_not_lose_data_flag)

    if args.indexSizing:
        (conf_files_requiring_changes, indexes_requiring_changes, calculated_size_total) = run_index_sizing(utility, index_list, args.indexNameRestriction, args.indexLimit,
        args.numberOfIndexers, args.lower_index_size_limit, args.sizing_continency, args.min_days_of_license_for_sizing, args.perc_before_adjustment, args.do_not_lose_data_flag,
        args.undersizing_continency, args.smallbucket_size, args.skipProblemIndexes, indexes_requiring_changes, conf_files_requiring_changes, args.rep_factor_multiplier, args.upper_comp_ratio_level,
        args.no_sizing_comments)

    if args.outputTempFilesWithTuning:
        indextuning_indextempoutput.output_index_files_into_temp_dir(conf_files_requiring_changes, index_list, args.workingPath, indexes_requiring_changes)

    if args.workWithGit:
        success = True
        if not args.gitRepoURL:
            logger.warn("git repo URL not supplied, not performing any git work")
            success = False

        if success:
            if os.path.isdir(args.gitWorkingDir):
                logger.debug("git directory exists, determine if git is working by running git status")
                (output, stderr, res) = utility.run_os_process("cd " + args.gitWorkingDir + args.gitRoot + "; git status", logger)

                if res == False:
                    logger.info("Failure!")
                    # Something failed? Easiest solution is to wipe the directory + allow re-clone to occur or similar
                    # if that fails we cancel the git component completely perhaps?!
                    logger.warn("git error occurred while attempting to work with dir={} stdout=\"{}\" stderr=\"{}\"".format(args.gitWorkingDir, output, stderr))
                    shutil.rmtree(args.gitWorkingDir)
                    os.makedirs(args.gitWorkingDir)
                    logger.debug("Attempting clone again from url={}".format(args.gitRepoURL))

                    if args.gitFirstConnection:
                        # This is a once off make it a switch?!
                        (output, stderr, res) = utility.run_os_process("ssh -n -o \"BatchMode yes\" -o StrictHostKeyChecking=no " + args.gitRepoURL[:args.gitRepoURL.find(":")], logger)
                        if res == False:
                            logger.warn("Unexpected failure while attempting to trust the remote git repo. stdout=\"%s\", stderr=\"%s\"" % (output, stderr))

                    (output, stderr, res) = utility.run_os_process("cd %s; git clone %s" % (args.gitWorkingDir, args.gitRepoURL), logger, timeout=120)
                    if res == False:
                        logger.warn("git clone failed for some reason...on url=%s stdout=\"%s\", stderr=\"%s\"" % (args.gitRepoURL, output, stderr))
                else:
                    logger.debug("git command result is res=%s" % (res))
                    logger.info("Success, git is working as expected")
            else:
                if not os.path.isdir(args.gitWorkingDir):
                    os.makedirs(args.gitWorkingDir)
                (output, stderr, res) = utility.run_os_process("cd %s; git clone %s" % (args.gitWorkingDir, args.gitRepoURL), logger, timeout=120)
                if res == False:
                    logger.warn("git clone failed for some reason...on url %s, output is '%s', stderr is '%s'" % (args.gitRepoURL, output, stderr))

            git_path = args.gitWorkingDir + args.gitRoot

            # Always start from master and the current version
            (output, stderr, res) = utility.run_os_process("cd %s; git checkout master; git pull" % (git_path), logger)
            if res == False:
                logger.warn("git checkout master or git pull failed, stdout=\"%s\" stderr=\"%s\"" % (output, stderr))
                # TODO below is copy and paste, should be a method/function
                logger.warn("git error occurred while attempting to work with dir={} stdout=\"{}\" stderr=\"{}\"".format(args.gitWorkingDir, output, stderr))
                shutil.rmtree(args.gitWorkingDir)
                os.makedirs(args.gitWorkingDir)
                logger.debug("git attempting clone again from url={}".format(args.gitRepoURL))

                if args.gitFirstConnection:
                    # This is a once off make it a switch?!
                    (output, stderr, res) = utility.run_os_process("ssh -n -o \"BatchMode yes\" -o StrictHostKeyChecking=no " + args.gitRepoURL[:args.gitRepoURL.find(":")], logger)
                    if res == False:
                        logger.warn("git unexpected failure while attempting to trust the remote git repo url=%s, stdout=\"%s\", stderr=\"%s\"" % (args.gitRepoURL, output, stderr))

                (output, stderr, res) = utility.run_os_process("cd %s; git clone %s" % (args.gitWorkingDir, args.gitRepoURL), logger, timeout=120)
                if res == False:
                    logger.warn("git clone failed for some reason...on url=%s stdout=\"%s\", stderr=\"%s\"" % (args.gitRepoURL, output, stderr))

            # At this point we've written out the potential updates
            indextuning_indextempoutput.output_index_files_into_temp_dir(conf_files_requiring_changes, index_list, git_path, indexes_requiring_changes, replace_slashes=False)
            (output, stderr, res) = utility.run_os_process("cd %s; git status | grep \"nothing to commit\"" % (git_path), logger)
            if res == False:
                # We have one or more files to commit, do something
                # Then we git checkout -b indextuning_20181220_1120
                todays_date = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
                (output, stderr, res) = utility.run_os_process("cd {0}; git checkout -b {1}_{2} 2>&1; git commit -am \"Updated by index auto-tuning algorithm on {2}\" 2>&1; git push origin {1}_{2} 2>&1".format(git_path, args.gitBranch, todays_date), logger)
                if res == False:
                    logger.warn("git failure while creating new branch and pushing to remote git repo stdout=\"%s\" stderr=\"%s\"" % (output, stderr))
                else:
                    logger.info("Changes commited into git and pushed without warnings, stdout=\"%s\", stderr=\"%s\"" % (output, stderr))
                    if args.gitLabToken and args.gitLabURL:
                        res = requests.post(args.gitLabURL,
                        headers = { 'Private-Token': args.gitLabToken },
                        data={ 'target_branch' : 'master',
                            'source_branch' : args.gitBranch + "_" + todays_date,
                            'title' : 'Automated merge request from index tuning script on ' + todays_date },
                        verify=False)

                        if res.status_code != requests.codes.ok and res.status_code != 201:
                            logger.error("git url=%s statuscode=%s reason=%s response=\"%s\"" % (args.gitLabURL, res.status_code, res.reason, res.text))
                        else:
                            logger.debug("gitlab res=\"%s\"" % (res.text))
                    elif args.gitLabToken:
                        logger.warn("gitLabToken supplied but the gitLabURL has not been provided, will not create a merge request")
            else:
                logger.info("No changes to be checked into git")

# If we asked for sizing estimates only, and we're not running the dead index check only option
if args.sizingEstimates:
    total_index_allocation = 0
    total_estimated_index_allocation = 0

    #Ugly hack until I find a better way to do this
    total_growth_per_day_mb = [0 for i in range(0,365)]

    for index in list(index_list.keys()):
        max_total_data_size_mb = index_list[index].max_total_data_size_mb
        total_index_allocation = total_index_allocation + max_total_data_size_mb
        logger.debug("index=%s max_total_data_size_mb=%s" % (index, max_total_data_size_mb))
        if hasattr(index_list[index], "estimated_total_data_size"):
            estimated_total_data_size = index_list[index].estimated_total_data_size
            logger.info("index=%s estimated_total_data_size=%s, perc_of_current_disk_utilised=%s, days_until_full_compared_to_frozen=%s, days_until_full_disk_calculation=%s, "\
                " current_max_on_disk=%s, estimated_total_data_size_with_contingency=%s, perc_utilised_on_estimate=%s, days_until_full_disk_calculation_on_estimate=%s"
                % (index, estimated_total_data_size, index_list[index].perc_utilised, index_list[index].days_until_full,
                index_list[index].days_until_full_disk_calculation, index_list[index].splunk_max_disk_usage_mb,
                index_list[index].estimated_total_data_size_with_contingency, index_list[index].perc_utilised_on_estimate,
                index_list[index].days_until_full_disk_calculation_on_estimate))

            # If the index is not yet full it will likely consume further disk space on the indexing tier...
            if index_list[index].days_until_full > 0:
                total_growth_per_day_calc = estimated_total_data_size / ((index_list[index].frozen_time_period_in_secs)/60/60/24)

                if index_list[index].days_until_full > 365:
                    days_until_full = 365
                else:
                    days_until_full = index_list[index].days_until_full
                for entry in range(days_until_full):
                    total_growth_per_day_mb[entry] = total_growth_per_day_mb[entry] + total_growth_per_day_calc

            total_estimated_index_allocation = total_estimated_index_allocation + estimated_total_data_size

    for index in list(indexes_not_getting_sized.keys()):
        max_total_data_size_mb = indexes_not_getting_sized[index].max_total_data_size_mb
        total_index_allocation = total_index_allocation + max_total_data_size_mb
        logger.debug("index=%s max_total_data_size_mb=%s (indexes_not_getting_sized)" % (index, max_total_data_size_mb))
        if hasattr(indexes_not_getting_sized[index], "estimated_total_data_size"):
            estimated_total_data_size = indexes_not_getting_sized[index].estimated_total_data_size
            logger.info("index=%s estimated_total_data_size=%s (indexes_not_getting_sized), perc_of_current_disk_utilised=%s, days_until_full_compared_to_frozen=%s, " \
                " days_until_full_disk_calculation=%s, current_max_on_disk=%s, estimated_total_data_size_with_contingency=%s, perc_utilised_on_estimate=%s, " \
                " days_until_full_disk_calculation_on_estimate=%s"
                % (index, estimated_total_data_size, indexes_not_getting_sized[index].perc_utilised, indexes_not_getting_sized[index].days_until_full,
                indexes_not_getting_sized[index].days_until_full_disk_calculation, indexes_not_getting_sized[index].splunk_max_disk_usage_mb,
                indexes_not_getting_sized[index].estimated_total_data_size_with_contingency, indexes_not_getting_sized[index].perc_utilised_on_estimate,
                indexes_not_getting_sized[index].days_until_full_disk_calculation_on_estimate))

            # If the index is not yet full it will likely consume further disk space on the indexing tier...
            if indexes_not_getting_sized[index].days_until_full > 0:
                total_growth_per_day_calc = estimated_total_data_size / ((indexes_not_getting_sized[index].frozen_time_period_in_secs)/60/60/24)

                if indexes_not_getting_sized[index].days_until_full > 365:
                    days_until_full = 365
                else:
                    days_until_full = indexes_not_getting_sized[index].days_until_full

                for entry in range(days_until_full):
                    total_growth_per_day_mb[entry] = total_growth_per_day_mb[entry] + total_growth_per_day_calc

            total_estimated_index_allocation = total_estimated_index_allocation + estimated_total_data_size

    total_vol_size = 0
    total_in_use_currently = 0
    for vol in list(vol_list.keys()):
        if hasattr(vol_list[vol], "max_vol_data_size_mb"):
            vol_size = vol_list[vol].max_vol_data_size_mb

            # Determine current disk utilisation for this volume
            stat = os.statvfs(vol_list[vol].path)
            used_in_mb = ((stat.f_blocks-stat.f_bfree)*stat.f_bsize)/1024/1024
            if vol != "_splunk_summaries":
                total_vol_size = total_vol_size + vol_size
                total_in_use_currently = total_in_use_currently + used_in_mb
            logger.info("volume=%s max_vol_data_size_mb=%s used_in_mb=%s" % (vol, vol_size, used_in_mb))
        else:
            logger.info("volume=%s, has no maxVolumedata_size_mb setting" % (vol))
    logger.info("Summary: total_index_allocated=%s total_volume_allocated=%s (excluding _splunk_summaries)" % (total_index_allocation, total_vol_size))

    total_available = total_vol_size - total_in_use_currently
    logger.debug("total_available=%s, total_vol_size=%s, total_in_use_currently=%s" % (total_available, total_vol_size, total_in_use_currently))

    day_counter = 0
    while total_available > 0 and day_counter < 365:
        total_available = total_available - total_growth_per_day_mb[day_counter]
        day_counter = day_counter + 1

    if day_counter >= 365:
        logger.info("Based on a combined available volume size of %s with %s in use currently, leaving %s available, I am calculating we will not run out of disk in the next 365 days"
            % (total_vol_size, total_in_use_currently, total_available))        
    else:
        logger.info("Based on a combined available volume size of %s with %s in use currently, leaving %s available, I am calculating %s days before we run out of disk"
            % (total_vol_size, total_in_use_currently, total_available, day_counter))


    if total_estimated_index_allocation > 0:
        if args.indexLimit < len(index_list):
            logger.warn("Estimated size cannot be accurate as we have looked at index_limit=%s of total_indexes=%s" % (args.indexLimit, len(index_list)))
        logger.info("estimated_index_allocation=%s" % (total_estimated_index_allocation))

if index_dir_check_res:
    hot_dirs_checked = list(index_dir_check_res["hot_dirs_checked"].keys())
    cold_dirs_checked = list(index_dir_check_res["cold_dirs_checked"].keys())
    summaries_dirs_checked = list(index_dir_check_res["summaries_dirs_checked"].keys())
    dead_hot_dirs = index_dir_check_res["hot_dirs_dead"]
    dead_cold_dirs = index_dir_check_res["cold_dirs_dead"]
    dead_summary_dirs = index_dir_check_res["summaries_dirs_dead"]

    #Note that duplicate values can be returned if the same path is used for hot/cold or summaries, for example /opt/splunk/var/lib/splunk
    #may be duplicated...
    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the indexes hot path=\"%s\"" % (hot_dirs_checked))
    if len(list(dead_hot_dirs.keys())) > 0:
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in list(dead_hot_dirs.keys()):
            for entry in dead_hot_dirs[line]:
                #We escaped spaces for the shell, but we do not want spaces escaped for python
                line = line.replace('\\ ',' ')
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    if os.path.isdir(thedir):
                        logger.info("Wiping directory %s" % (thedir))
                        shutil.rmtree(thedir)
                    else:
                        logger.warn("dir=%s does not exist, no deletion required" % (thedir))
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead hot dirs found")

    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the indexes cold path=\"%s\"" % (cold_dirs_checked))
    if len(list(dead_cold_dirs.keys())) > 0:
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in list(dead_cold_dirs.keys()):
            for entry in dead_cold_dirs[line]:
                #We escaped spaces for the shell, but we do not want spaces escaped for python
                line = line.replace('\\ ',' ')
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    if os.path.isdir(thedir):
                        logger.info("dir=%s deleted due to not existing and deadIndexDelete flag enabled" % (thedir))
                        shutil.rmtree(thedir)
                    else:
                        logger.warn("dir=%s does not exist, no deletion required" % (thedir))
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead cold dirs found")

    #output the remaining data around "dead" indexes which have directories on the filesystem but no matching config
    logger.info("The following directories were checked to ensure that they are still in use by Splunk indexes in the summaries path=\"%s\"" % (summaries_dirs_checked))
    if len(list(dead_summary_dirs.keys())) > 0:
        logger.info("The below list were located in the above directories but no mention in the btool output, these should likely be removed from the filesystem:")
        for line in list(dead_summary_dirs.keys()):
            for entry in dead_summary_dirs[line]:
                #We escaped spaces for the shell, but we do not want spaces escaped for python
                line = line.replace('\\ ',' ')
                thedir = entry + "/" + line
                if args.deadIndexDelete and not line == "\\$_index_name":
                    if os.path.isdir(thedir):
                        logger.info("dir=%s deleted due to not existing and deadIndexDelete flag enabled" % (thedir))
                        shutil.rmtree(thedir)
                    else:
                        logger.warn("dir=%s does not exist, no deletion required" % (thedir))
                else:
                    logger.info(thedir)
    else:
        logger.info("No dead summary dirs found")

    if args.deadIndexDelete:
        unique_directories_checked = hot_dirs_checked + cold_dirs_checked + summaries_dirs_checked
        unique_directories_checked = list(set(unique_directories_checked))

        for a_dir in unique_directories_checked:
            sub_dir_list = utility.listdirs(a_dir)
            for a_sub_dir in sub_dir_list:
                try:
                    os.rmdir(a_sub_dir)
                except OSError as e:
                    continue
                logger.info("dir=%s directory was deleted" % (a_sub_dir))

    if len(list(dead_hot_dirs.keys())) > 0 and not args.deadIndexDelete:
        for line in list(dead_hot_dirs.keys()):
            for entry in dead_hot_dirs[line]:
                print(entry + "/" + line)

    if len(list(dead_cold_dirs.keys())) > 0 and not args.deadIndexDelete:
        for line in list(dead_cold_dirs.keys()):
            for entry in dead_cold_dirs[line]:
                print(entry + "/" + line)

    if len(list(dead_summary_dirs.keys())) > 0 and not args.deadIndexDelete:
        for line in list(dead_summary_dirs.keys()):
            for entry in dead_summary_dirs[line]:
                print(entry + "/" + line)

logger.info("End index sizing script with args=\"%s\"" % (clean_args))
