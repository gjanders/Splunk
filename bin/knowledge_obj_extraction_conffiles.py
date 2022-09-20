import logging
from logging.config import dictConfig
import argparse
import os
import csv
import urllib.parse
import sys
import shutil

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
parser.add_argument('-migrateUserDirs', help='Copy the $SPLUNK_HOME/etc/users/<app> as well into outputDir?', action='store_true')
parser.add_argument('-outputAppName', help='Specify the app to be created in $outputDir/users/<appname>/config, defaults to the -app name passed in if not specified')

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
        stanza_name = ""
        # read through each line of the file
        for line in fp:
            logger.debug(f"Working with line={line} file={conf_file}")
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
def parse_conf_files(app_dir, splunk_type, app, filter_list, filter_type):
    # these types do not have .conf files as such
    if splunk_type == "lookups" or splunk_type == "views":
        logger.info("Skipping views/lookups in conf file reading, this is only required for metadata")
        return [], []

    # attempt to parse the default/<type>.conf files
    conf_file = f"{app_dir}/default/{splunk_type}.conf"
    string_res_list_default = parse_single_conf_file(conf_file, app, splunk_type, filter_type, filter_list)

    # attempt to parse the local/<type>.conf files
    conf_file = f"{app_dir}/local/{splunk_type}.conf"
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

local_dir = f'{args.outputDir}/apps/local'
if not os.path.isdir(local_dir):
    os.makedirs(local_dir, exist_ok=True)

metadata_dir = f'{args.outputDir}/apps/metadata'
if not os.path.isdir(metadata_dir):
    os.mkdir(metadata_dir)

default_dir = f'{args.outputDir}/apps/default'
if not os.path.isdir(default_dir):
    os.mkdir(default_dir)

lookups_dir = f'{args.outputDir}/apps/lookups'

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

user_dirs_to_check = []
if args.migrateUserDirs:
    user_dirs = os.listdir(f"{args.splunkhome}/etc/users")
    logger.debug(f"Checking user_dirs={user_dirs}")
    for user in user_dirs:
        a_user_dir = f"{args.splunkhome}/etc/users/{user}"
        if not os.path.isdir(a_user_dir):
            continue
        if args.app in os.listdir(a_user_dir):
            user_dir_found = f"{a_user_dir}/{args.app}"
            logger.debug(f"Appending directory user_dir_found={user_dir_found}")
            user_dirs_to_check.append(user_dir_found)

# each splunk_type is a separate configuration file
for a_type in splunk_type:
    app_dir = f"{args.splunkhome}/etc/apps/{args.app}"
    string_res_list_default, string_res_list = parse_conf_files(app_dir, a_type, args.app, filter_list, filter_type)

    if len(string_res_list) > 0:
        with open(f'{local_dir}/{a_type}.conf', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")

    if len(string_res_list_default) > 0:
        with open(f'{default_dir}/{a_type}.conf', 'w') as f:
            for line in string_res_list_default:
                f.write(f"{line}")

    # deal with user-level objects too
    if args.migrateUserDirs:
        for a_dir in user_dirs_to_check:
            string_res_list_default, string_res_list = parse_conf_files(a_dir, a_type, args.app, filter_list, filter_type)
            if len(string_res_list) > 0:
                user_dir = os.path.basename(os.path.dirname(a_dir))
                if args.outputAppName:
                    output_app_name = args.outputAppName
                else:
                    output_app_name = args.app
                dest_dir = f"{args.outputDir}/users/{user_dir}/{output_app_name}"
                os.makedirs(f"{dest_dir}/local", exist_ok=True)
                with open(f'{dest_dir}/local/{a_type}.conf', 'w') as f:
                    for line in string_res_list:
                        f.write(f"{line}")

logger.info("knowledge object extraction ends")

logger.info("Metadata extraction begins")

# there is only 1 default metadata file for all types of config
default_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/default.meta"
if not os.path.isfile(default_meta_file):
    logger.warning(f"file={default_meta_file} does not exist skipping this file")
else:
    string_res_list = parse_metadata_file(default_meta_file, args.app, splunk_type, filter_type, filter_list_encoded)
    if len(string_res_list) > 0:
        with open(f'{metadata_dir}/default.meta', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")


# there is only 1 local metadata file for all types of config
local_meta_file = f"{args.splunkhome}/etc/apps/{args.app}/metadata/local.meta"
if not os.path.isfile(local_meta_file):
    logger.warning(f"file={local_meta_file} does not exist skipping this file")
else:
    string_res_list = parse_metadata_file(local_meta_file, args.app, splunk_type, filter_type, filter_list_encoded)
    if len(string_res_list) > 0:
        with open(f'{metadata_dir}/local.meta', 'w') as f:
            for line in string_res_list:
                f.write(f"{line}")

# if we are migrating user directories we may need user metadata too
if args.migrateUserDirs:
    for a_dir in user_dirs_to_check:
        user_dir = os.path.basename(os.path.dirname(a_dir))
        string_res_list = parse_metadata_file(f"{a_dir}/metadata/local.meta", args.app, splunk_type, filter_type, filter_list_encoded)

        if len(string_res_list) > 0:
            if args.outputAppName:
                output_app_name = args.outputAppName
            else:
                output_app_name = args.app
            metadata_dir = f"{args.outputDir}/users/{user_dir}/{output_app_name}/metadata"
            os.makedirs(metadata_dir, exist_ok=True)
            with open(f"{metadata_dir}/local.meta", 'w') as f:
                for line in string_res_list:
                    f.write(f"{line}")

logger.info("Metadata extraction ends")

################
#
# Copy other files (i.e. files that exist on the filesystem
#
################
default_src_dir = f"{args.splunkhome}/etc/apps/{args.app}/default"
local_src_dir = f"{args.splunkhome}/etc/apps/{args.app}/local"
lookups_src_dir = f"{args.splunkhome}/etc/apps/{args.app}/lookups"

def find_and_copy_files(dir, dest_dir, type, extension, filter_type, filter_list):
    if os.path.isdir(dir):
        files = os.listdir(dir)
        for file_name in files:
            lookup = f"{type}_{file_name[0:file_name.find(extension)]}"
            if len(filter_list) > 0 and type in filter_type and not lookup in filter_list:
                logger.info(f"dir={dir} file={file_name} lookup={lookup} did not match filter list, skipping")
            else:
                logger.debug(f"copying file dir={dir} file={file_name} lookup={lookup} to dest_dir={dest_dir}")
                if not os.path.isdir(dest_dir):
                    # create all required irectories
                    os.makedirs(dest_dir, exist_ok=True)
                if not os.path.isfile(f"{dir}/{file_name}"):
                    logger.info(f"dir={dir} file={file_name} is not a file, skipping")
                    continue
                shutil.copy2(f"{dir}/{file_name}", dest_dir)

for type in splunk_type:
    if type == "datamodels":
        datamodels_src = f"{default_src_dir}/data/models"
        datamodels_dest = f"{default_dir}/data/models"
        find_and_copy_files(datamodels_src, datamodels_dest, "datamodels", ".json", filter_type, filter_list)
        datamodels_src = f"{local_src_dir}/data/models"
        datamodels_dest = f"{local_dir}/data/models"
        find_and_copy_files(datamodels_src, datamodels_dest, "datamodels", ".json", filter_type, filter_list)
    elif type == "lookups":
        find_and_copy_files(lookups_src_dir, lookups_dir, "lookups", ".csv", filter_type, filter_list)
    elif type == "panels":
        panels_src = f"{default_src_dir}/data/ui/panels"
        panels_dst = f"{default_dir}/data/ui/panels"
        find_and_copy_files(panels_src, panels_dst, "panels", ".xml", filter_type, filter_list)
        panels_src = f"{local_src_dir}/data/ui/panels"
        panels_dst = f"{local_dir}/data/ui/panels"
        find_and_copy_files(panels_src, panels_dst, "panels", ".xml", filter_type, filter_list)
    elif type=="views":
        views_src = f"{default_src_dir}/data/ui/views"
        views_dst = f"{default_dir}/data/ui/views"
        find_and_copy_files(views_src, views_dst, "views", ".xml", filter_type, filter_list)
        views_src = f"{local_src_dir}/data/ui/views"
        views_dst = f"{local_dir}/data/ui/views"
        find_and_copy_files(views_src, views_dst, "views", ".xml", filter_type, filter_list)
    elif type=="nav":
        default_nav = f"{default_src_dir}/data/ui/nav/default.xml"
        if os.path.isfile(default_nav):
            nav_dir = f"{default_dir}/data/ui/nav"
            if not os.path.isdir(nav_dir):
                os.makedirs(nav_dir, exist_ok=True)
            shutil.copy2(default_nav, nav_dir)
        local_nav = f"{local_src_dir}/data/ui/views/default.xml"
        if os.path.isfile(local_nav):
            nav_dir = f"{local_dir}/data/ui/nav"
            if not os.path.isdir(nav_dir):
                os.makedirs(nav_dir, exist_ok=True)
            shutil.copy2(local_nav, nav_dir)

# if we are migrating user directories we may need other files as well
if args.migrateUserDirs:
    for a_dir in user_dirs_to_check:
        user_dir = os.path.basename(os.path.dirname(a_dir))

        if args.outputAppName:
            output_app_name = args.outputAppName
        else:
            output_app_name = args.app

        for type in splunk_type:
            if type == "datamodels":
                datamodels_src = f"{a_dir}/local/data/models"
                datamodels_dest = f"{args.outputDir}/users/{user_dir}/{output_app_name}/local/data/models"
                logger.debug(f"datamodels_src={datamodels_src} datamodels_dest={datamodels_dest}")
                find_and_copy_files(datamodels_src, datamodels_dest, "datamodels", ".json", filter_type, filter_list)
            elif type == "lookups":
                lookups_src = f"{a_dir}/lookups"
                lookups_dest = f"{args.outputDir}/users/{user_dir}/{output_app_name}/lookups"
                logger.debug(f"src={lookups_src} dest={lookups_dest}")
                find_and_copy_files(lookups_src, lookups_dest, "lookups", ".csv", filter_type, filter_list)
            elif type == "panels":
                panels_src = f"{a_dir}/local/data/ui/panels"
                panels_dst = f"{args.outputDir}/users/{user_dir}/{output_app_name}/local/data/ui/panels"
                logger.debug(f"src={panels_src} dest={panels_dst}")
                find_and_copy_files(panels_src, panels_dst, "panels", ".xml", filter_type, filter_list)
            elif type=="views":
                views_src = f"{a_dir}/local/data/ui/views"
                views_dst = f"{args.outputDir}/users/{user_dir}/{output_app_name}/local/data/ui/views"
                logger.debug(f"src={views_src} dest={views_dst}")
                find_and_copy_files(views_src, views_dst, "views", ".xml", filter_type, filter_list)
