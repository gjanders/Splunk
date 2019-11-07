import six.moves.urllib.request, six.moves.urllib.parse, six.moves.urllib.error
import requests
from xml.dom import minidom
import re
import json
from subprocess import Popen, PIPE, check_output
import threading
import six.moves.queue
from time import sleep
import sys
import os
import logging
from io import open

logger = logging.getLogger()

# Splunk volume
class volume:
    def __init__(self, name):
        self.name = name


# Splunk index
class index:
    def __init__(self, name):
        self.name = name


# Index Tuning Utility Class
# runs queries against Splunk or Splunk commands
class utility:
    username = ""
    password = ""
    splunkrest = ""
    earliest_time = ""

    def __init__(self, username, password, splunkrest, earliest_time):
        self.username = username
        self.password = password
        self.splunkrest = splunkrest
        self.earliest_time = earliest_time

    # Run a search query against the Splunk restful API
    def run_search_query(self, searchQuery):
        baseurl = 'https://' + self.splunkrest

        # For troubleshooting
        logger.debug("SearchQuery=\n" + searchQuery)

        # Run the search
        url = baseurl + '/services/search/jobs'
        data = {'search': searchQuery, 'earliest_time': self.earliest_time,
                'latest_time': 'now', 'output_mode': "json",
                "exec_mode": "oneshot", "timeout": 0 }
        logger.debug("Running against URL=%s with username=%s "\
            "with data_load=%s" % (url, self.username, data))
        res = requests.post(url, auth=(self.username, self.password),
                            data=data, verify=False)

        if res.status_code != requests.codes.ok:
            logger.error("Failed to run search on URL=%s with username=%s "\
            "response_code=%s, reason=%s, text=%s, payload=%s"
                          % (url, self.username, res.status_code,
                             res.reason, res.text, data))
            sys.exit(-1)

        logger.debug("Result from query=%s" % (res.text))

        return res.text

    ##############
    #
    # Read the btool output of splunk btool indexes list --debug
    #
    ##############
    # Run the btool command and parse the output
    def parse_btool_output(self):
        #  Keep a large dictionary of the indexes and associated information
        indexes = {}
        volumes = {}

        # Run python btool and read the output, use debug switch to obtain
        # file names
        logger.debug("Running /opt/splunk/bin/splunk btool indexes list --debug")
        output = check_output(["/opt/splunk/bin/splunk", "btool", "indexes",
                              "list", "--debug"])
        output = output.split("\n")

        # If we are currently inside a [volume...] stanza or not...
        disabled = False

        # Work line by line on the btool output
        for line in output:
            # don't attempt to parse empty lines
            if line == "":
                continue

            logger.debug("Working with line=" + line)
            # Search for the [<indexname>]
            # Split the string /opt/splunk/etc/slave-apps/_cluster/local/indexes.conf                [_internal]
            # into 2 pieces
            regex = re.compile(r"^([^ ]+) +(.*)")
            result = regex.match(line)
            conf_file = result.group(1)
            string_res = result.group(2)
            logger.debug("conf_file=%s, string_res=%s" % (conf_file, string_res))
            # Further split a line such as:
            # homePath.maxDataSizeMB = 0
            # into 2 pieces...
            # we utilise this later in the code
            regex2 = re.compile(r"^([^= ]+)\s*=\s*(.*)")

            stanza = ""
            value = ""
            if string_res[0] == "[":
                # Skip volume entries
                if string_res.find("[volume") == -1:
                    # Slice it out of the string
                    index_name = string_res[1:len(string_res)-1]
                    cur_index = index(index_name)
                    cur_index.conf_file = conf_file
                    logger.debug("Working with index=" + str(cur_index))
                else:
                    # skip the volume:... part and the ] at the end
                    vol_name = string_res[8:len(string_res)-1]
                    vol = volume(vol_name)
                    vol.conf_file = conf_file
                    logger.debug("Working with volume=" + vol_name)
                # We are done with this round of the loop
                continue
            else:
                result2 = regex2.match(string_res)
                stanza = result2.group(1)
                value = result2.group(2)

            logger.debug("Working with stanza=%s and value=%s" % (stanza, value))
            if stanza == "disabled":
                if value == "1":
                    disabled = True
            # Path exists only within volumes
            elif stanza == "path":
                vol.path = value
            elif stanza == "coldPath.maxDataSizeMB":
                cur_index.coldpath_max_datasize_mb = float(value)
            elif stanza == "coldToFrozenDir":
                if value != "":
                    cur_index.cold_to_frozen_dir = value
            elif stanza == "datatype":
                cur_index.datatype = value
            elif stanza == "frozenTimePeriodInSecs":
                cur_index.frozen_time_period_in_secs = int(value)
            elif stanza == "homePath.maxDataSizeMB":
                cur_index.homepath_max_data_size_mb = float(value)
            elif stanza == "homePath":
                cur_index.home_path = value
            elif stanza == "coldPath":
                cur_index.cold_path = value
            elif stanza == "maxDataSize":
                cur_index.max_data_size = value
                if cur_index.max_data_size.find("auto_high_volume") != -1:
                    cur_index.max_data_size = "10240_auto"
                elif cur_index.max_data_size.find("auto") != -1:
                    cur_index.max_data_size = "750_auto"
            elif stanza == "maxHotBuckets":
                cur_index.max_hot_buckets = float(value)
            # This setting only appears in volumes
            elif stanza == "maxVolumeDataSizeMB":
                vol.max_vol_data_size_mb = int(value)
                logger.debug("Recording vol=%s into volumes dict" % (vol.name))
                volumes[vol.name] = vol
            elif stanza == "maxTotalDataSizeMB":
                cur_index.max_total_data_size_mb = int(value)
            elif stanza == "thawedPath":
                cur_index.thawed_path = value
            # btool prints in lexicographical order so therefore we can assume
            # tstatsHomePath is last for an index
            elif stanza == "tstatsHomePath":
                # Do not record disabled indexes
                if disabled:
                    disabled = False
                    continue
                cur_index.tstats_home_path = value
                # btool uses alphabetical order so this is the last entry in
                # the list
                indexes[cur_index.name] = cur_index
                logger.debug("Recording index=%s into indexes dict" % (cur_index.name))

        return indexes, volumes

    #####################
    #
    # Comment lines specific to this environment, pull out the lines that we
    # need related to # .. @ 1GB/day ...
    #
    #####################
    # Example lines to find the bits of
    # maximum storage for all buckets (6 months @ 1GB/day @ 50% (15% + 35%)
    # compression)
    # maximum storage for all buckets (2 months @ 0.1GB/day @ 50% (15% + 35%)
    # compression)
    def parse_conf_files_for_sizing_comments(self, indexes, conf_files):
        find_comments_rex = re.compile(r"^#[^@]+@\s+([0-9\.]+)\s*([^/]+)/([^ ]+)")

        for a_file in list(conf_files.keys()):
            with open(a_file) as file:
                index_name = ""

                for line in file:
                    if line.find("[volume:") != -1:
                        pass
                    # Now looking at an index entry and not a volume entry
                    elif line.find("[") == 0:
                        start = line.find("[")+1
                        end = line.find("]")
                        index_name = line[start:end]
                    # comments with sizing have @ symbols, nothing else
                    # has the @ symbol in the btool indexes list output
                    elif line.find("@") != -1:
                        result = find_comments_rex.match(line)
                        if not result:
                            continue
                        # Size as in number (0.1 for example), unit as
                        # GB perX as in day or similar
                        size = result.group(1)
                        unit = result.group(2).upper()
                        # perX = result.group(3)
                        calc_size = 0
                        if unit == "GB":
                            calc_size = int(float(size)*1024)
                        elif unit == "TB":
                            calc_size = int(float(size)*1024*1024)
                        else:
                            # Assume MB
                            calc_size = int(size)

                        # Record the size in MB
                        logger.debug("index=%s found size=%s, unit=%s, calculated=%s" % (index_name, size, unit, calc_size))
                        indexes[index_name].size_per_day_in_mb = calc_size
    ################
    #
    # End environment specific
    #
    ################

    """
     Determine for the specified index the ratio of the index size compared to
     the raw data volume overall (monitoring console keeps these stats)
     this works just fine *if* the number of hours is valid, if there are data
     parsing issues then it may look like a bucket has 6000 hours worth of
     data but in reality it's just timestamp parsing problem(s)
    """
    def determine_recommended_bucket_size(self, index_name, num_hours_per_bucket):
        # added rawSize>0 as streaming hot buckets appear to show 0 rawSize but
        # a sizeOnDiskMB is measured so this results in an unusually large
        # MB/hour number!
        http_result = self.run_search_query(
            " | dbinspect index=%s | eval hours=(endEpoch-startEpoch)/60/60 "\
            " | where hours>1 AND rawSize>0 | eval sizePerHour=sizeOnDiskMB/hours " \
            " | stats avg(sizePerHour) AS averageSizePerHour " \
            " | eval bucket_size=averageSizePerHour*%s " \
            " | fields bucket_size" % (index_name, num_hours_per_bucket))

        # Load the result so that it's formatted into a dictionary instead of string
        logger.debug("Bucket size http_result=" + http_result)
        json_result = json.loads(http_result)

        if len(json_result["results"]) != 1:
            logger.info("No results found for index=%s with dbinspect command" % (index_name))
            return float(0)

        bucket_size = float(json_result["results"][0]["bucket_size"])
        logger.debug("index=%s bucket_size=%s")

        return bucket_size

    # Over a time period determine how much license was used on average per day,
    # maxmimum during the period and also return number of days of license data
    def determine_license_usage_per_day(self, index_name, earliest_license,
                                    latest_license):
        # Generic query
        http_result = self.run_search_query(
            "search index=_internal source=*license_usage.log sourcetype=splunkd "\
            "earliest=%s latest=%s idx=%s "\
            "| bin _time span=1d "\
            "| stats sum(b) AS totalBytes by idx, _time "\
            "| stats avg(totalBytes) AS avgBytesPerDay, "\
            "  max(totalBytes) AS maxBytesPerDay, earliest(_time) AS firstSeen "\
            "| eval avgMBPerDay=round(avgBytesPerDay/1024/1024), "\
            "  maxMBPerDay=round(maxBytesPerDay/1024/1024), "\
            "  firstSeen=(now()-firstSeen)/60/60/24 "\
            "| fields avgMBPerDay, maxMBPerDay, firstSeen" \
            % (earliest_license, latest_license, index_name))

        logger.debug("index=%s earliest_time=%s latest_time=%s http_result=%s" % (index_name, earliest_license, latest_license, http_result))
        # Load the result so that it's formatted into a dictionary instead of string
        json_result = json.loads(http_result)

        if len(json_result["results"]) != 1:
            logger.info("No results found for license query for index=%s earliest_time=%s" % (index_name, earliest_license))
            return 0, 0, 0

        # We just want the avgMBPerDay in license usage terms
        avg_mb_per_day = json_result["results"][0]["avgMBPerDay"]
        days_of_license_usage = json_result["results"][0]["firstSeen"]
        max_mb_per_day = json_result["results"][0]["maxMBPerDay"]

        logger.debug("determine_license_usage_per_day recording index=%s avg_mb_per_day=%s days_of_license_usage=%s max_mb_per_day=%s" % (index_name, avg_mb_per_day, days_of_license_usage, max_mb_per_day))
        # We return the average MB per day, days of license usage available and the max, all as the int type
        return int(avg_mb_per_day), int(float(days_of_license_usage)), int(max_mb_per_day)

    """
     There are many potential ways to determine the index compression ratio,
     multiple options were considered
     * Use dbinspect and view the last 14 days, there are 2 flaws with this
       logic, the first is that this will show all buckets modified during
       the last 14 days
       The second issue is that dbinspect shows the parsed time, so any data
       with time parsing will have unusual earliest/latest times so we cannot
       confirm what data was added during that time period, and therefore
       cannot accurately calculate how much data the last 2 weeks
       of license usage used
     * The second option was to use introspection data for the growth info
       this works fine *except* we cannot determine how much was deleted
       therefore we cannot accurately calculate the data size for the
       license period we measure

       Note that the REST API also works instead of the introspection index but
       this was faster and slightly easier in some ways...
    """
    def determine_compression_ratio(self, index_name, indexerhostnamefilter,
                                    use_introspection_data):
        # Introspection data is quicker but slightly less accurate for the
        # earliest time, therefore it's a trade off in terms of
        # which one we choose
        if use_introspection_data:
            http_result = self.run_search_query(
                "search earliest=-10m index=_introspection component=Indexes host=%s data.name=%s "\
                "| eval minTime=coalesce('data.bucket_dirs.cold.event_min_time', 'data.bucket_dirs.home.event_min_time'), "\
                " maxTime=coalesce('data.bucket_dirs.home.event_max_time', 'data.bucket_dirs.cold.event_max_time') "\
                "| stats first(data.total_size) AS total_size, "\
                "  first(data.total_raw_size) AS total_raw_size, "\
                "  min(minTime) AS earliest_time, "\
                "  max(maxTime) AS newest_time by data.name, host "\
                "| stats sum(total_size) AS total_size, "\
                "  sum(total_raw_size) AS total_raw_size, "\
                "  max(total_size) AS max_total_size, "\
                "  min(earliest_time) AS earliest_time, "\
                "  max(newest_time) AS newest_time by data.name "\
                "| eval ratio=total_size/total_raw_size, "\
                "  earliest_time = ceiling((now() - earliest_time) / 86400) , "\
                "  newest_time = floor((now() - newest_time) / 86400)" \
                "| eval earliest_time = if(isnotnull(earliest_time), "\
                "  earliest_time, 0) "\
                "| fields ratio, max_total_size, earliest_time, newest_time"
                % (indexerhostnamefilter, index_name))
        else:
            search = "| rest /services/data/indexes/%s splunk_server=%s "\
                    "| join title splunk_server type=outer "\
                    "[| rest /services/data/indexes-extended/%s splunk_server=%s]" % (index_name, indexerhostnamefilter, index_name, indexerhostnamefilter)

            search = search + """| eval minTime=strptime(minTime,"%Y-%m-%dT%H:%M:%S%z"), maxTime=strptime(maxTime,"%Y-%m-%dT%H:%M:%S%z")
                    | stats sum(currentDBSizeMB) AS total_size,
                     max(currentDBSizeMB) AS max_total_size,
                     sum(total_raw_size) AS total_raw_size,
                     min(minTime) AS earliest_time
                     max(maxTime) AS newest_time
                    | eval ratio=total_size/total_raw_size,
                      earliest_time = ceiling((now() - earliest_time) / 86400),
                      newest_time = floor((now() - newest_time) / 86400)
                    | eval earliest_time = if(isnotnull(earliest_time), earliest_time, 0)
                    | fields ratio, max_total_size, earliest_time, newest_time"""
            http_result = self.run_search_query(search)

        logger.debug("determine_compression_ratio index_name=%s use_introspection_data=%s http_result=%s"% (index_name, use_introspection_data, http_result))
        # Load the result so that it's formatted into a dictionary instead of string
        json_result = json.loads(http_result)

        # If we don't have both results we have no data for this index
        if "results" not in json_result or len(json_result["results"]) != 1:
            logger.error("No results='%s' from querying index=%s with indexhostnamefilter=%s" % (json_result, index_name, indexerhostnamefilter))
            # Return a comp ratio of 0.5 as a guess just in case...
            return float(0.5), float(0), int(0), int(0)

        if len(json_result["results"][0]) != 4:
            logger.error("Unexpected results, expected 4 results and got results='%s' "\
                "from querying index=%s with indexhostnamefilter=%s"
                          % (json_result, index_name, indexerhostnamefilter))
            # Return a comp ratio of 0.5 as a guess just in case...
            return float(0.5), float(0), int(0), int(0)

        ratio = float(json_result["results"][0]["ratio"])
        maxtotalsize = float(json_result["results"][0]["max_total_size"])
        earliest_time = json_result["results"][0]["earliest_time"]
        newest_time = json_result["results"][0]["newest_time"]

        logger.debug("determine_compression_ratio index=%s ratio=%s maxtotalsize=%s earliest_time=%s newest_time=%s" % (index_name, ratio, maxtotalsize, earliest_time, newest_time))

        return ratio, maxtotalsize, earliest_time, newest_time

    # List only directories and not files under a particular directory
    def listdirs(self, dir):
        return [d for d in os.listdir(dir) if os.path.isdir(os.path.join(dir, d))]

    # If copying and pasting into the shell we want $ symbols escaped and space symbols escaped
    def replace_dollar_symbols(self, thestr):
        if thestr.find("$") != -1 or thestr.find(" ") != -1:
            logger.debug(r"Replacing $ symbol with \$ and ' ' with '\ ' in case someone copies this output into a shell command")
            thestr = thestr.replace(r"$", r"\$")
            thestr = thestr.replace(" ", r"\ ")
        return thestr

    # Run an OS process with a timeout, this way if a command gets "stuck"
    # waiting for input it is killed
    # Had inconsistent results using Popen without a threaded process
    # thanks to https://stackoverflow.com/questions/6893968/how-to-get-the-return-value-from-a-thread-in-python
    def run_os_process(self, command, logger, timeout=20):
        def target(q):
            logger.debug("Begin OS process run of command=\"%s\"" % (command))
            process = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)
            (stdoutdata, stderrdata) = process.communicate()
            if process.returncode != 0:
                logger.debug("OS process exited with non-zero_code=%s \
                    , for command=\"%s\"" % (process.returncode, command))
                q.put(stdoutdata)
                q.put(stderrdata)
                q.put(False)
            else:
                logger.debug("OS process exited with code=0, for command=\"%s\"" % (command))
                q.put(stdoutdata)
                q.put(stderrdata)
                q.put(True)

        #Keep the arguments in the queue for use once the thread finishes
        q = six.moves.queue.Queue()
        thread = threading.Thread(target=target, args=(q,))
        thread.daemon = False
        thread.start()
        thread.result_queue = q
        thread.join(timeout)
        if thread.is_alive():
            process.terminate()
            thread.join()
            logger.warn("OS timeout=%s seconds while running command=\"%s\"" % (timeout, command))
            return "", "timeout after %s seconds" % (timeout), False
        logger.debug("Successful run of OS process command=\"%s\" within timeout=%s" % (command, timeout))

        return q.get(), q.get(), q.get()
