import urllib
import requests
from xml.dom import minidom
import re
import json
from subprocess import Popen, PIPE, check_output
from time import sleep
import sys
import os
import logging

#Index Tuning Utility Class
#runs queries against Splunk or Splunk commands
class utility:
    username = ""
    password = ""
    splunkrest = ""
    earliesttime = ""
    logging = logging.getLogger("utility")

    def __init__(self, username, password, splunkrest, earliesttime):
        self.username = username
        self.password = password
        self.splunkrest = splunkrest
        self.earliesttime = earliesttime

    #Run a search query against the Splunk restful API
    def runSearchQuery(self, searchQuery):
        baseurl = 'https://' + self.splunkrest

        #For troubleshooting
        logging.debug("Search Query is:\n" + searchQuery)

        # Run the search
        url = baseurl + '/services/search/jobs'
        data = {'search': searchQuery, 'earliest_time': self.earliesttime, 'latest_time': 'now', 'output_mode' : "json", "exec_mode": "oneshot"}
        logging.debug("Running against URL %s with username %s with data load %s" % (url, self.username, data))
        res = requests.post(url, auth=(self.username,self.password), data=data, verify=False)
        
        if (res.status_code != requests.codes.ok):
            logging.error("Failed to run search on URL %s with username %s response code of %s, reason %s, text %s, payload %s" % (url, self.username, res.status_code, res.reason, res.text, data))
            sys.exit(-1)
        
        logging.debug("Result from query is %s" % (res.text))
        
        return res.text

    ##############
    #
    # Read the btool output of splunk btool indexes list --debug
    #
    ##############
    #Run the btool command and parse the output
    def parseBtoolOutput(self):
        #Keep a large dictionary of the indexes and associated information
        indexList = {}
        volList = {}

        output = check_output(["/opt/splunk/bin/splunk", "btool", "indexes", "list", "--debug"])
        output = output.split("\n")

        #If we are currently inside a [volume...] stanza or not...
        confFile = ""
        confFileForIndexEntry = ""
        maxHotBuckets = ""
        indexName = ""
        coldPathMaxDataSizeMB = "0"
        frozenTimePeriodInSecs = "0"
        homePathMaxDataSizeMB = "0"
        homePath = ""
        coldPath = ""
        tstatsHomePath = ""
        volName = ""
        datatype = ""
        
        #Work line by line on the btool output
        for line in output:
            #don't attempt to parse empty lines
            if line == "":
                continue

            #Search for the [<indexname>]
            #Split the string /opt/splunk/etc/slave-apps/_cluster/local/indexes.conf                [_internal]
            #into 2 pieces
            regex = re.compile("^([^ ]+) +(.*)")
            result = regex.match(line)
            confFile = result.group(1)
            stringRes = result.group(2)        

            #Further split a line such as:
            #homePath.maxDataSizeMB = 0
            #into 2 pieces...
            #we utilise this later in the code
            regex2 = re.compile("^([^= ]+)\s*=\s*(.*)")

            stanza = ""
            value = ""
            if (stringRes[0] == "["):
                #Skip volume entries
                if (stringRes.find("[volume") == -1):
                    #Slice it out of the string
                    indexName = stringRes[1:len(stringRes)-1]
                    confFileForIndexEntry = confFile
                else:
                    #skip the volume:... part and the ] at the end
                    volName = stringRes[8:len(stringRes)-1] 
                #We are done with this round of the loop
                continue
            else:
                result2 = regex2.match(stringRes)
                stanza = result2.group(1)
                value = result2.group(2)

            #Path exists only within volumes
            if (stanza == "path"):
                volList[volName]["path"] = value
            elif (stanza == "coldPath.maxDataSizeMB"):
                coldPathMaxDataSizeMB = float(value)
            elif (stanza == "datatype"):
                datatype = value
            elif (stanza == "frozenTimePeriodInSecs"):
                frozenTimePeriodInSecs = value
            elif (stanza == "homePath.maxDataSizeMB"):
                homePathMaxDataSizeMB = float(value)
            elif (stanza == "homePath"):
                homePath = value
            elif (stanza == "coldPath"):
                coldPath = value
            elif (stanza == "maxDataSize"):
                maxDataSize = value
                if maxDataSize.find("auto_high_volume") != -1:
                    maxDataSize = "10240_auto"
                elif maxDataSize.find("auto") != -1:
                    maxDataSize = "750_auto"
            elif (stanza == "maxHotBuckets"):
                maxHotBuckets = float(value)
            #This setting only appears in volumes
            elif (stanza == "maxVolumeDataSizeMB"):
                volList[volName] = {}
                volList[volName]["maxVolumeDataSizeMB"] = int(value)
            elif (stanza == "maxTotalDataSizeMB"):
                maxTotalDataSizeMB = int(value)
            #btool prints in lexicographical order so therefore we can assume tstatsHomePath is last
            elif (stanza == "tstatsHomePath"):
                tstatsHomePath = value
                #btool uses alphabetical order so this is the last entry in the list
                indexList[indexName] = {"maxDataSize" : maxDataSize, "confFile" : confFileForIndexEntry, "maxHotBuckets": maxHotBuckets, "coldPathMaxDataSizeMB" : coldPathMaxDataSizeMB,
                "frozenTimePeriodInSecs" : frozenTimePeriodInSecs, "homePathMaxDataSizeMB" : homePathMaxDataSizeMB, "maxTotalDataSizeMB" : maxTotalDataSizeMB, "homePath" : homePath, 
                "coldPath" : coldPath, "tstatsHomePath": tstatsHomePath, "datatype" : datatype }
                 
        return indexList, volList

    #####################
    #
    # Comment lines specific to this environment, pull out the lines that we need related to # .. @ 1GB/day ...
    #
    #####################
    #Example lines to find the bits of
    # maximum storage for all buckets (6 months @ 1GB/day @ 50% (15% + 35%) compression)
    # maximum storage for all buckets (2 months @ 0.1GB/day @ 50% (15% + 35%) compression)
    def parseConfFilesForSizingComments(self, indexList, confFilesToCheck):
        findComments = re.compile("^#[^@]+@\s+([0-9\.]+)\s*([^/]+)/([^ ]+)")

        for aFile in confFilesToCheck.keys():
            with open(aFile) as file:

                indexName=""
                skipVol = False

                for line in file:
                    if (line.find("[volume:") != -1):
                        skipVol = True
                    #Now looking at an index entry and not a volume entry
                    elif (line.find("[") == 0):
                        start = line.find("[")+1
                        end = line.find("]")
                        indexName = line[start:end]
                        skipVol = False
                    #comments with sizing have @ symbols, nothing else has the @ symbol in the btool indexes list output
                    elif (line.find("@") != -1):
                        result = findComments.match(line)
                        if (not result):
                            continue
                        #Size as in number (0.1 for example), unit as GB perX as in day or similar
                        size = result.group(1)
                        unit = result.group(2).upper()
                        #perX = result.group(3)
                        calcSize = 0
                        if unit == "GB":
                            calcSize = int(float(size)*1024)
                        elif unit == "TB":
                            calcSize = int(float(size)*1024*1024)
                        else:
                            #Assume MB
                            calcSize = int(size)
                        
                        #Record the size in MB
                        indexList[indexName]["sizePerDayInMB"] = calcSize
    ################
    #
    # End environment specific
    #
    ################

    #Determine for the specified index the ratio of the index size compared to the raw data volume overall (monitoring console keeps these stats)
    #this works just fine *if* the number of hours is valid, if there are data parsing issues then it may look like a bucket has 6000 hours worth of data but in reality it's just timestamp parsing problem(s)
    def determineRecommendedBucketSize(self, indexName, numHoursPerBucket):
        #added rawSize>0 as streaming hot buckets appear to show 0 rawSize but a sizeOnDiskMB is measured so this results in an unusually large MB/hour number!
        httpResult = self.runSearchQuery('| dbinspect index=%s | eval hours=(endEpoch-startEpoch)/60/60  | where hours>1 AND rawSize>0 | eval sizePerHour=sizeOnDiskMB/hours | stats avg(sizePerHour) AS averageSizePerHour | eval bucketSize=averageSizePerHour*%s | fields bucketSize' % (indexName,numHoursPerBucket))
        
        #Load the result so that it's formatted into a dictionary instead of string
        jsonResult = json.loads(httpResult)
        
        if len(jsonResult["results"]) != 1:
            return float(0)

        bucketSize = float(jsonResult["results"][0]["bucketSize"])
        
        return bucketSize

    #Over a time period determine how much license was used on average per day, maxmimum during the period and also return number of days of license data
    def determineLicenseUsagePerDay(self, indexName, earliestLicense, latestLicense):
        httpResult = self.runSearchQuery('search index=_internal source=*license_usage.log sourcetype=splunkd earliest=%s latest=%s idx=%s | bin _time span=1d | stats sum(b) AS totalBytes by idx, _time | stats avg(totalBytes) AS avgBytesPerDay, max(totalBytes) AS maxBytesPerDay, earliest(_time) AS firstSeen | eval avgMBPerDay=round(avgBytesPerDay/1024/1024), maxMBPerDay=round(maxBytesPerDay/1024/1024), firstSeen=(now()-firstSeen)/60/60/24 | fields avgMBPerDay, maxMBPerDay, firstSeen' % (earliestLicense,latestLicense,indexName))

        #Load the result so that it's formatted into a dictionary instead of string
        jsonResult = json.loads(httpResult)

        if len(jsonResult["results"]) != 1:
            return 0, 0, 0

        #We just want the avgMBPerDay in license usage terms 
        avgMBPerDay = jsonResult["results"][0]["avgMBPerDay"]
        daysOfLicenseUsage = jsonResult["results"][0]["firstSeen"]
        maxMBPerDay = jsonResult["results"][0]["maxMBPerDay"]

        #We return the average MB per day, days of license usage available and the max, all as the int type
        return int(avgMBPerDay), int(float(daysOfLicenseUsage)), int(maxMBPerDay)

    #There are many potential ways to determine the index compression ratio, multiple options were considered
    # * Use dbinspect and view the last 14 days, there are 2 flaws with this logic, the first is that will show all buckets modified during the last 14 days
    #   The second issue is that dbinspect shows the parsed time, so any data with time parsing will have unusual earliest/latest times so we cannot confirm
    #   what data was added during that time period, and therefore cannot accurately calculate how much data the last 2 weeks of license usage used

    # * The second was to use introspection data for the growth information, this works fine *except* we cannot determine how much was deleted
    #   therefore we cannot accurately calculate the data size for the license period we measure

    #Note that the REST API also works instead of the introspection index but this was faster and slightly easier in some ways...
    #Determine the index compression ratio
    def determineCompressionRatio(self, indexName, indexerhostnamefilter, useIntrospectionData):
        #Introspection data is quicker but slightly less accurate for the earliest time, therefore it's a trade off in terms of which one we choose
        if useIntrospectionData:
            httpResult = self.runSearchQuery('search earliest=-10m index=_introspection component=Indexes host=%s data.name=%s | eval minTime=coalesce(\'data.bucket_dirs.cold.event_min_time\', \'data.bucket_dirs.home.event_min_time\') | stats first(data.total_size) AS total_size, first(data.total_raw_size) AS total_raw_size, min(minTime) AS earliestTime by data.name, host | stats sum(total_size) AS total_size, sum(total_raw_size) AS total_raw_size, max(total_size) AS max_total_size, min(earliestTime) AS earliestTime by data.name | eval ratio=total_size/total_raw_size, earliestTime = ceiling((now() - earliestTime) / 86400) | eval earliestTime = if(isnotnull(earliestTime), earliestTime, 0) | fields ratio, max_total_size, earliestTime' % (indexerhostnamefilter,indexName))
        else:
            search = """| rest /services/data/indexes/%s splunk_server=%s
                    | join title splunk_server type=outer 
                        [| rest /services/data/indexes-extended/%s splunk_server=%s]""" % (indexName, indexerhostnamefilter, indexName, indexerhostnamefilter)
            search = search + """| eval minTime=strptime(minTime,"%Y-%m-%dT%H:%M:%S%z") 
                    | stats sum(currentDBSizeMB) AS total_size, max(currentDBSizeMB) AS max_total_size, sum(total_raw_size) AS total_raw_size, min(minTime) AS earliestTime
                    | eval ratio=total_size/total_raw_size, earliestTime = ceiling((now() - earliestTime) / 86400)
                    | eval earliestTime = if(isnotnull(earliestTime), earliestTime, 0)
                    | fields ratio, max_total_size, earliestTime"""
            httpResult = self.runSearchQuery(search)
        
        #Load the result so that it's formatted into a dictionary instead of string
        jsonResult = json.loads(httpResult)

        #If we don't have both results we have no data for this index
        if len(jsonResult["results"]) != 1:
            logging.error("no results: '%s'" % (jsonResult))
            return float(0), float(0), int(0)
        if len(jsonResult["results"][0]) != 3:
            return float(0), float(0), int(0)

        logging.debug("Result of determineCompressionRatio is: " + str(jsonResult))
        ratio = float(jsonResult["results"][0]["ratio"])
        maxtotalsize = float(jsonResult["results"][0]["max_total_size"])
        earliestTime = jsonResult["results"][0]["earliestTime"]

        return ratio, maxtotalsize, earliestTime

    #List only directories and not files under a particular directory
    def listdirs(self, dir):
        return [d for d in os.listdir(dir) if os.path.isdir(os.path.join(dir, d))]

    #If copying and pasting into the shell we want $ symbols escaped and space symbols escaped
    def replaceDollarSymbols(self, thestr):
        if (thestr.find("$") != -1 or thestr.find(" ") != -1):
            #logging.debug("Replacing $ symbol with \$ and ' ' with '\ ' in case someone copies this output into a shell command")
            thestr = thestr.replace("$", "\$")
            thestr = thestr.replace(" ", "\ ")
        return thestr
        
    def runOSProcess(self, command, timeout=10):
        logging.debug("Now running command '%s'" % (command))
        p = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)
        for t in xrange(timeout):
            sleep(1)
            if p.poll() is not None:
                #return p.communicate()
                (stdoutdata, stderrdata) = p.communicate()
                if p.returncode != 0:
                    return stdoutdata, stderrdata, False
                else:
                    return stdoutdata, stderrdata, True
        p.kill()
        return "", "timeout after %s seconds" % (timeout), False