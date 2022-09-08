from subprocess import Popen, PIPE, check_output
import re
import logging
from logging.config import dictConfig
import argparse
import os
from configparser import RawConfigParser
import csv
import urllib.parse
import sys

####################################################################################################
#
# knowledge_obj_extraction_conffiles
#
#  The idea of this script is to extract configuration from .conf files and metadata files
#  and output the result to a new directory
#
#  A filter.csv file can be used the format can be:
#   type, name
#   savedsearches,mysavedsearch
#   views,myview
#
#  If a type is not in the filter then all are included by default (i.e. if you don't have a views line, all views are included)
#
####################################################################################################

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
              'filename' : '/tmp/knowledgeobj_extraction.log',
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
parser = argparse.ArgumentParser(description='Print splunk configuration from savedsearches or other files to standard out, ignoring system default config')
parser.add_argument('-splunkhome', help='Directory of SPLUNK_HOME so that bin/splunk btool can be run or etc/apps/metadata can be parsed', required=False, default="/opt/splunk")
parser.add_argument('-type', help='Type of knowledge object to extract', required=True, choices=['app','collections','commands','datamodels','eventtypes','lookups','macros','panels','props','savedsearches','tags','times','transforms','views','workflow_actions', 'all'])
parser.add_argument('-app', help='Splunk app to extract the knowledge object from', required=True)
parser.add_argument('-filterCSV', help='A CSV list to filter the names of the objects, lines should be type/name, or just the name if filtering without the "all" type', required=False)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-outputDir', help='Directory to output files to', required=True)

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

logger.info("knowledge object extraction starts")

# parse a single config file and extract any filtered lines
def parse_single_conf_file(conf_file, app, splunk_type, filter_type, filter_list):
    string_res_list = []

    # if the file does not exist in this app we just return an empty list
    if not os.path.isfile(conf_file):
        logger.info(f"conf_file={conf_file} not found in app={app}")
        return string_res_list

    with open(conf_file, "r") as fp:
        # read through each line of the file
        for line in fp:
            logger.debug(f"Working with line={line}")
            # vsid's are legacy configuration related to viewstates, we don't need them
            if line.find("vsid") == 0:
                logger.info(f"skipping vsid line, line={line}")
                continue

            # it's a stanza line [mysearch]
            if len(line) > 0 and line[0] == "[":
                stanza_name = line[1:len(line)-2]
                logger.debug(f"working with stanza={stanza_name}")

            # if we are in the stanza name that is filtered, don't output the lines from it
            # we filter only if we have >0 entries for the said filter_type (i.e. we might filter savedsearches but not views/lookups/et cetera)
            filter_lookup = f"{splunk_type}_{stanza_name}"
            if len(filter_list) > 0 and splunk_type in filter_type and not filter_lookup in filter_list:
                logger.info(f"stanza={stanza_name} with filter={filter_lookup} not found in filter list, skipping")
                continue

            string_res_list.append(line)
    return string_res_list

# parse all conf files for an app
def parse_conf_files(splunkhome, splunk_type, app, filter_list, filter_type):
    # these types do not have .conf files as such
    if splunk_type == "lookups" or splunk_type == "views":
        logger.info("Skipping views/lookups in conf file reading, this is only required for metadata")
        return [], []

    # attempt to parse the default/<type>.conf files
    conf_file = f"{splunkhome}/etc/apps/{app}/default/{splunk_type}.conf"
    string_res_list_default = parse_single_conf_file(conf_file, app, splunk_type, filter_type, filter_list)

    # attempt to parse the local/<type>.conf files
    conf_file = f"{splunkhome}/etc/apps/{app}/local/{splunk_type}.conf"
    string_res_list = parse_single_conf_file(conf_file, app, splunk_type, filter_type, filter_list)

    return string_res_list_default, string_res_list

# parse metadata files, these are slightly different to conf files
def parse_metadata_file(metadata_file, app, splunk_type, filter_type, filter_list):
    string_res_list = []
    if not os.path.isfile(metadata_file):
        logger.info(f"metadata_file={metadata_file} not found in app={app}")
        return string_res_list

    with open(metadata_file, "r") as fp:
        for line in fp:
            logger.debug(f"Working with line={line}")
            if len(line) > 0 and line[0] == "[":
                stanza_name = line[1:len(line)-2]
                logger.debug(f"working with stanza={stanza_name}")

                # we may have [props] or we might have [savedsearches/mysearch]
                if stanza_name.find("/") != -1:
                    stanza_type = stanza_name[0:stanza_name.find("/")]
                else:
                    stanza_type = stanza_name
                logger.debug(f"stanza_type={stanza_type}")

            if not stanza_type in splunk_type and not stanza_name=="":
                logger.info(f"skipping splunk_type={splunk_type} stanza_type={stanza_type}")
                continue
            # check the filter list
            elif len(filter_list) > 0 and stanza_type in filter_type and not stanza_name in filter_list:
                logger.info(f"stanza={stanza_name} not found in filter list, skipping")
                continue

            # hack to change system to none / prevent global objects unless requested
            if line.find("export = system") == 0:
                line="export = none"

            string_res_list.append(line)
    return string_res_list

# all is the alias for just about any configuration we want to retrieve from the metadata/config excluding viewstates
if args.type=='all':
    splunk_type = ['app','collections','commands','datamodels','eventtypes','lookups','macros','panels','props','savedsearches','tags','times','transforms','views','workflow_actions','nav']
else:
    splunk_type = [ args.type ]

# Create the directories we will need
if not os.path.isdir(args.outputDir):
    os.mkdir(args.outputDir)

local_dir = f'{args.outputDir}/local'
if not os.path.isdir(local_dir):
    os.mkdir(local_dir)

metadata_dir = f'{args.outputDir}/metadata'
if not os.path.isdir(metadata_dir):
    os.mkdir(metadata_dir)

default_dir = f'{args.outputDir}/default'
if not os.path.isdir(default_dir):
    os.mkdir(default_dir)

################
#
# Setup filtering
#
################
filter_list = []
filter_list_encoded = []
filter_type = {}
if args.filterCSV:
    with open(args.filterCSV, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if 'type' in row:
                logger.debug(f"Reading type={row['type']}, name={row['name']} from file={args.filterCSV}")
                filter_list.append(f"{row['type']}_{row['name']}")
                # the encoded version is for the metadata file, metadata url encodes the stanza names
                encoded_name = urllib.parse.quote(row['name'])
                filter_list_encoded.append(f"{row['type']}/{encoded_name}")
                logger.debug(f"Adding {row['type']}/{encoded_name} to the encoded list")
                # add to dict for later filtering
                filter_type[row['type']] = None
            else:
                if args.type=='all':
                    logger.error("args.type=all however CSV does not have a type and name column, these are required to filter with all")
                    sys.exit(1)
                logger.debug(f"Reading {args.type}_{row['name']} from file={args.filterCSV}")
                filter_list.append(f"{args.type}_{row['name']}")
                # the encoded version is for the metadata file, metadata url encodes the stanza names
                encoded_name = urllib.parse.quote(row['name'])
                filter_list_encoded.append(f"{args.type}/{encoded_name}")
                logger.debug(f"Adding {args.type}/{encoded_name} to the encoded list")
                filter_type[args.type] = None

# keep a list of filtered types for when we parse the config
filter_type = list(filter_type.keys())

# each splunk_type is a separate configuration file
for a_type in splunk_type:
    string_res_list_default, string_res_list = parse_conf_files(args.splunkhome, a_type, args.app, filter_list, filter_type)

    if len(string_res_list) > 0:
        with open(f'{local_dir}/{a_type}.conf', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")

    if len(string_res_list_default) > 0:
        with open(f'{default_dir}/{a_type}.conf', 'w') as f:
            for line in string_res_list_default:
                f.write(f"{line}")

logger.info("knowledge object extraction ends")

logger.info("Metadata extraction begins")

default_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/default.meta"
if not os.path.isfile(default_meta_file):
    logger.warning(f"file={default_meta_file} does not exist skipping this file")
else:
    # there is only 1 default metadata file for all types of config
    string_res_list = parse_metadata_file(default_meta_file, args.app, splunk_type, filter_type, filter_list_encoded)
    if len(string_res_list) > 0:
        with open(f'{metadata_dir}/default.meta', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")

local_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/local.meta"
if not os.path.isfile(local_meta_file):
    logger.warning(f"file={local_meta_file} does not exist skipping this file")
else:
    # there is only 1 local metadata file for all types of config
    string_res_list = parse_metadata_file(local_meta_file, args.app, splunk_type, filter_type, filter_list_encoded)
    if len(string_res_list) > 0:
        with open(f'{metadata_dir}/local.meta', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")
