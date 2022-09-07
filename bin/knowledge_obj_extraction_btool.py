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
parser.add_argument('-splunkhome', help='Directory of SPLUNK_HOME so that bin/splunk btool can be run or etc/apps/metadata can be parsed', required=False, default="/opt/app/splunk")
parser.add_argument('-type', help='Type of knowledge object to extract', required=True, choices=['app','collections','commands','datamodels','eventtypes','lookups','macros','panels','props','savedsearches','tags','times','transforms','views','workflow_actions', 'all'])
parser.add_argument('-app', help='Splunk app to extract the knowledge object from', required=True)
parser.add_argument('-filterCSV', help='A CSV list to filter the names of the objects, lines should be type/name, or just the name if filtering without the "all" type', required=False)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-outputDir', help='Directory to output files to', required=True)
parser.add_argument('-doNotMergeDefault', help='(optional) if enabled then default/savedsearches.conf is separate from local/savedsearches.conf (otherwise only local exists)', action='store_true')

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

logger.info("knowledge object extraction starts")

# Run the btool command and parse the output
def parse_btool_output(splunkhome, splunk_type, app, do_not_merge_default, filter_list, filter_type):
    # Run python btool and read the output, use debug switch to obtain
    # file names
    if splunk_type == "lookups" or splunk_type == "views":
        logger.info("Skipping views/lookups in btool, this is only required for metadata")
        return [], []
    logger.debug(f"Running {splunkhome}/bin/splunk btool {splunk_type} --list --debug")
    output = check_output([f"{splunkhome}/bin/splunk", "btool", splunk_type,
                          "list", "--debug", f"--app={app}"]).decode('utf-8')
    output = output.split("\n")

    # if we end with a newline remove it from the list
    if output[-1] == "":
        output.pop()
    # Work line by line on the btool output
    string_res_list = []
    string_res_list_default = []
    backslash_required = True
    last_conf_file = ""
    stanza_printed = False

    for line in output:
        logger.debug(f"Working with line={line}")

        # Search for the [<indexname>]
        # Split the string /opt/splunk/etc/slave-apps/_cluster/local/indexes.conf                [_internal]
        # into 2 pieces
        regex = re.compile(r"^(/[^ ]+) +(.*)")
        result = regex.match(line)
        if not result:
            logger.debug(f"No match on line={line} assuming the whole line is the result...")
            string_res = line
            conf_file = last_conf_file
        else:
            conf_file = result.group(1)
            last_conf_file = conf_file
            string_res = result.group(2)

        if do_not_merge_default and conf_file.find("/default/") != -1:
            default_file = True
        else:
            default_file = False

        if string_res.find("vsid") == 0:
            logger.info(f"skipping vsid line, string_res={string_res} line={line}")
            continue

        if len(string_res) > 0 and string_res[0] == "[":
            stanza_name = string_res[1:len(string_res)-1]
            logger.debug(f"working with stanza={stanza_name}")
            
            # check the filter list and if we find a match don't add empty lines in
            filter_lookup = f"{splunk_type}_{stanza_name}"
            if len(filter_list) > 0 and splunk_type in filter_type and not filter_lookup in filter_list:
                logger.debug(f"stanza={stanza_name} with filter={filter_lookup} not found in filter list, skipping")
                continue
            
            if default_file:
                string_res_list_default.append("")
                stanza_printed = True
            else:
                string_res_list.append("")
                stanza_printed = False
                if len(string_res_list_default) > 0 and string_res_list_default[-1] != "":
                    string_res_list_default.append("")

        # if we are in the stanza name that is filtered, don't output the lines from it
        filter_lookup = f"{splunk_type}_{stanza_name}"
        if len(filter_list) > 0 and splunk_type in filter_type and not filter_lookup in filter_list:
            logger.debug(f"stanza={stanza_name} with filter={filter_lookup} not found in filter list, skipping")
            continue

        if line == "":
            # blank lines can be kept with the \ on them
            if line.find(splunkhome) != 0:
                if default_file:
                    if not stanza_printed:
                        string_res_list_default.append(f"[{stanza_name}]")
                        stanza_printed = True
                    string_res_list_default.append(string_res_list_default.pop() + "\\")
                else:
                    string_res_list.append(string_res_list.pop() + "\\")
                backslash_required = False

            if default_file:
                if not stanza_printed:
                    string_res_list_default.append(f"[{stanza_name}]")
                    stanza_printed = True
                string_res_list_default.append("\\")
            else:
                string_res_list.append("\\")
            continue
        logger.debug(f"conf_file={conf_file}, string_res={string_res}")

        if backslash_required and line.find(splunkhome) != 0:
            if default_file:
                if not stanza_printed:
                    string_res_list_default.append(f"[{stanza_name}]")
                    stanza_printed = True
                string_res_list_default.append(string_res_list_default.pop() + "\\")
            else:
                string_res_list.append(string_res_list.pop() + "\\")
            backslash_required = False

        if line.find(splunkhome) != 0:
            backslash_required = True
        
        if default_file:
            if not stanza_printed:
                string_res_list_default.append(f"[{stanza_name}]")
                stanza_printed = True
            string_res_list_default.append(string_res)
        else:
            string_res_list.append(string_res)
    return string_res_list_default, string_res_list

if args.type=='all':
    splunk_type = ['app','collections','commands','datamodels','eventtypes','lookups','macros','panels','props','savedsearches','tags','times','transforms','views','workflow_actions']
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
if args.doNotMergeDefault and not os.path.isdir(default_dir):
    os.mkdir(default_dir)

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
                encoded_name = urllib.parse.quote(f"{args.type}_{row['name']}")
                filter_list_encoded.append(encoded_name)
                filter_list_encoded.append(f"{args.type}/{encoded_name}")
                logger.debug(f"Adding {args.type}/{encoded_name} to the encoded list")
                filter_type[args.type] = None

filter_type = list(filter_type.keys())

#
# Run btool 
#
for a_type in splunk_type:
    string_res_list_default, string_res_list = parse_btool_output(args.splunkhome, a_type, args.app, args.doNotMergeDefault, filter_list, filter_type)
    #print(string_res_list)
    #print(*string_res_list, sep = "\n")
    if len(string_res_list) > 0:
        with open(f'{local_dir}/{a_type}.conf', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}\n")

    if args.doNotMergeDefault:
        if len(string_res_list_default) > 0:
            with open(f'{default_dir}/{a_type}.conf', 'w') as f:
                for line in string_res_list_default:
                    f.write(f"{line}\n")

logger.info("knowledge object extraction ends")

logger.info("Metadata extraction begins")

# remove the [] stanza entry and leave all others
def readline_generator(fp, splunk_type, filter_list, filter_type):
    line = fp.readline()
    skip = False
    stanza = ""
    while line:
        if line[0] == "[":
            stanza = line[1:len(line)-1]
            if stanza.find(f"{splunk_type}/") == 0:
                logger.debug(f"stanza matches type expected, type={splunk_type} stanza={stanza} line={line}")
                skip = False
            else:
                skip = True

        if stanza != "":
            filter_name = stanza[:-1]
        else:
            filter_name = ""

        if stanza == "]":
            skip = True            
        elif len(filter_list) > 0 and splunk_type in filter_type and not filter_name in filter_list:
            logger.debug(f"type={splunk_type} stanza={stanza} line={line} however it was not in the filtered list so skipping this entry")
            skip = True
        if not skip:
            logger.debug(f"readline_gen={line}")
            yield line
        line = fp.readline()

configur = RawConfigParser(strict=False)
    
# if we are included default.metadata files...
if args.doNotMergeDefault:
    configur_default = RawConfigParser(strict=False)
    # this is not efficient but this script is only used a few times...
    for a_type in splunk_type:
        default_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/default.meta"
        if not os.path.isfile(default_meta_file):
            logger.warning(f"file={default_meta_file} does not exist skipping this file")
            continue
        # read the default.meta first        
        logger.debug(f"Opening file {default_meta_file} for parsing")
        fp = open(default_meta_file, 'r')
        configur_default.read_file(readline_generator(fp, a_type, filter_list_encoded, filter_type))
        fp.close()
else:
    # this is not efficient but this script is only used a few times...
    for a_type in splunk_type:    
        # read the default.meta first
        default_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/default.meta"
        if not os.path.isfile(default_meta_file):
            logger.warning(f"file={default_meta_file} does not exist skipping this file")
            continue        
        logger.debug(f"Opening file {default_meta_file} for parsing")
        fp = open(default_meta_file, 'r')
        configur.read_file(readline_generator(fp, a_type, filter_list_encoded, filter_type))
        fp.close()

for a_type in splunk_type:    
    # let local.meta override default.meta where appropriate
    local_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/local.meta"
    if not os.path.isfile(local_meta_file):
        logger.warning(f"file={local_meta_file} does not exist skipping this file")
        continue    
    logger.debug(f"Opening file {local_meta_file} for parsing")
    fp = open(local_meta_file, 'r')
    configur.read_file(readline_generator(fp, a_type, filter_list_encoded, filter_type))
    fp.close()
    
#print ("Sections : ", configur.sections())

# write out the new (potentially) combined metadata file for migration
with open(f'{metadata_dir}/local.meta', 'w') as f:
  configur.write(f)

if args.doNotMergeDefault:
    with open(f'{metadata_dir}/default.meta', 'w') as f:
        configur_default.write(f)
        
if os.path.isfile(f'{metadata_dir}/local.meta') and os.stat(f'{metadata_dir}/local.meta').st_size == 0:
    os.remove(f'{metadata_dir}/local.meta')
if os.path.isfile(f'{metadata_dir}/default.meta') and os.stat(f'{metadata_dir}/default.meta').st_size == 0:
    os.remove(f'{metadata_dir}/default.meta')
