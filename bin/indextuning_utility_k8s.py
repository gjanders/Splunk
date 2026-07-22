import six.moves.urllib.request, six.moves.urllib.parse, six.moves.urllib.error
import requests
from xml.dom import minidom
import re
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
                              "list", "--debug"]).decode('utf-8')
        output = output.split("\n")

        # If we are currently inside a [volume...] stanza or not...
        disabled = False

        # Work line by line on the btool output
        for line in output:
            # don't attempt to parse empty lines
            if line == "":
                continue

            logger.debug("Working with line=%s" % (line))
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
                    in_index_mode = True
                    logger.debug("Working with index=" + str(cur_index))
                else:
                    # skip the volume:... part and the ] at the end
                    vol_name = string_res[8:len(string_res)-1]
                    vol = volume(vol_name)
                    vol.conf_file = conf_file
                    in_index_mode = False
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
            elif stanza == "deleted":
                if value == "true":
                    disabled = True
            # Path exists only within volumes
            elif stanza == "path" and not in_index_mode:
                vol.path = value
            elif stanza == "homePath" and in_index_mode:
                cur_index.home_path = value
            elif stanza == "coldPath" and in_index_mode:
                cur_index.cold_path = value
            elif stanza == "thawedPath" and in_index_mode:
                cur_index.thawed_path = value
            elif stanza == "tstatsHomePath" and in_index_mode:
                # Do not record disabled indexes
                if disabled:
                    disabled = False
                    continue
                cur_index.tstats_home_path = value

                if not hasattr(cur_index, "home_path"):
                    logger.warn("index=%s does not have a homePath, not recording this index" % index_name)
                    continue
                # btool uses alphabetical order so this is the last entry in
                # the list
                indexes[cur_index.name] = cur_index
                logger.debug("Recording index=%s into indexes dict" % (cur_index.name))

        return indexes, volumes
