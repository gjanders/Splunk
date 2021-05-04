import logging
from logging.config import dictConfig
import argparse
import os
import fnmatch
import json
import sys
from configparser import ConfigParser
import os
import fileinput
import re

"""
  splunk-props-transforms-routing
    This script takes a json configuration file, and a directory
    Based on the directory it reads the props.conf/transforms.conf files and then checks
    to see if the said json configuration exists in the props/transforms
    In check mode it just advises if it found in, in simulation mode it prints log messages
    to advise of what would change. In update mode it actually updates the files...
    Note this script assumes it is running on Linux

  Tested using splunk cmd python with version 2.7.16 / Splunk 8.0.0, older python versions may not have the configparser.ConfigParser...
  you can fix this by changing the ConfigParser line to:
  from ConfigParser import SafeConfigParser as ConfigParser
  And removing the strict= entries on each ConfigParser...
"""

#TODO do this in python, this makes ConfigParser use UTF-8 and not throw strange errors...
os.environ['LC_CTYPE'] = 'en_US.UTF-8'
os.environ['LANG'] = 'en_US.UTF-8'

"""
 ConfigParser unfortunately strips comments from the file it is parsing which is fine for reading the files
 but a problem when we output the file, we don't want to strip the comments.
 This was a potential Unix solution so leaving it here as a record of what could be done, but will not use ConfigParser
 for output of the .conf files...
 Note that someone line articles advise to change comment_prefix to a new character but that has it's own issues and results in files not parsing

  diff --ignore-matching-lines='^[^#].*' x2 x1 > x.patch
  The patch command will advise the above file is garbage because it misses a line, if this is appended to the top of the file:
  --- x1
  +++ x2

  Then:
  patch < x.patch

  Will work and comments will be added back in, however the alternative option is to not use ConfigParser for output'ing the files and that is what
  this script will be doing
"""

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
              'filename' : '/tmp/splunk-props-transforms-routing.log',
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

# Create the argument parser
parser = argparse.ArgumentParser(description='Create or confirm the routing configuration to particular outputs based on a yaml configuration file', epilog=\
"""The JSON based config file follows a particular format...:
In the below example it routes apache:access (a sourcetype) to the syslog-type output of forwarders_45704 but only for the index example
Other options available include: output_type: TCP, config: nullQueue, output_name: nullQueue, default_output: default_group_from_outputs.conf, name: optional_override_name, add_mode: <any value>, remove_mode: <any value>
You can comma separate as many entries as you wish within the JSON file (whitespace is lost when printing help FYI):
{
    "apache:access":{
        "output_name":"forwarders_45704",
        "output_type":"syslog",
        "config":"route",
        "index_name":"example"
    }
}
Note that each name (in the above apache:access) must be unique, but you can use the name: entry to apply multiple routing options to a single sourcetype if required
Also note that if add_mode exists then the new setting will be appended to the existing settings if they exist. i.e. if _TCP_ROUTING=default,example and output_name=example2, all 3 will be added.
Otherwise _TCP_ROUTING=example2 (or default,example2 if output_default was set to default)
remove_mode is the opposite of add mode, it removes the _TCP_ROUTING setting if it exists or removes the required entry from _TCP_ROUTING
""")
parser.add_argument('-location', help='[dir] The directory where the props/transforms/inputs are located, props/transforms/inputs will be found recursively', required=True)
parser.add_argument('-mode', help='Should this check for the expected config, show what would get updated or update props/transforms/inputs', required=True, choices=['check','simulate','update'])
parser.add_argument('-configfile', help='[file] JSON-based config file used to determine what to check/configure', required=True)
parser.add_argument('-configdir', help='[dir] If there is no current props.conf, transforms.conf files in which directory should it be created (this dir must exist)', required=False)
parser.add_argument('-debugMode', help='Enable debug logging for the script', required=False, choices=['true'])

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

# Provided a list of files with a section (stanza) and potentially the line within the stanza
# return the filename in which the changes need to occur
def get_config_file(config_files, section, option=None):
    found = False

    for file in config_files:
        ind_config = ConfigParser(allow_no_value=True, strict=False)

        # perform case sensitive matching
        ind_config.optionxform = str
        ind_config.read(file)
        if ind_config.has_section(section):
            if option and ind_config.has_option(section, option):
                logger.info("Within file=%s found stanza=%s for setting containing a TRANSFORMS- entry=%s" % (file, section, option))
                found = True
                break
            elif not option:
                logger.info("Within file=%s found stanza=%s" % (file, section))
                found = True
                break

    # under normal circumstances this should not happen...
    if not found:
        file = None
        logger.error("Unable to find section=%s option=%s in files config_files=%s" % (section, option, config_files))
    return file

# Provided with a filename, the stanza, the entry within the stanza and the updated line
# Update the file in-place with the updatedline
def update_config_file(filename, stanza, entry, updated_line, new_entry=False):
    cur_stanza = None
    new_entry_written = False

    for line in fileinput.input(filename, inplace=True):
        logger.debug("line=%s" % (line[:-1]))
        if line.find("[") == 0:
            # We have not written the new_entry and we hit a new stanza, insert it above the new stanza
            if new_entry and cur_stanza == stanza and not new_entry_written:
                line = entry + " = " + updated_line + "\n" + line
                logger.debug("Updated line=%s" % (line[:-1]))
                new_entry_written = True
            cur_stanza = line[1:line.find("]")]
            logger.debug("stanza=%s within file=%s" % (cur_stanza, filename))

        if cur_stanza == stanza:
           logger.debug("In stanza=%s in filename=%s" % (cur_stanza, filename))
           if not new_entry and re.match(r"^" + entry + "\s*=",line) != None:
               logger.info("file=%s line=%s will become line=%s = %s within stanza=%s" % (filename, line[:-1], entry, updated_line, cur_stanza))
               line = entry + " = " + updated_line + "\n"
           elif new_entry and re.match(r"^\s*$", line) != None and not new_entry_written:
               logger.info("file=%s line=%s will have %s = %s added within stanza=%s" % (filename, line[:-1], entry, updated_line, cur_stanza))
               line = entry + " = " + updated_line + "\n" + line
               new_entry_written = True
        # python 2, do not add extra newlines...
        print line,
    if new_entry and not new_entry_written:
        logger.info("Hit end of file but have not written this line, %s = %s, it will be written to file=%s at the end (in stanza=%s)" % (entry, updated_line, filename, stanza))
        with open(filename, 'a') as file:
            file.write("%s = %s" % (entry, updated_line))

# Provided with a filename, and the stanza
# comment out the said stanza...
def comment_config_file(filename, stanza, target_entry=False):
    cur_stanza = None

    for line in fileinput.input(filename, inplace=True):
        logger.debug("line=%s" % (line[:-1]))
        if line.find("[") == 0:
            cur_stanza = line[1:line.find("]")]
            logger.debug("stanza=%s within file=%s" % (cur_stanza, filename))

        if cur_stanza == stanza and line.find("#") != 0 and len(line)>1:
            if (not target_entry) or (target_entry and line.find(target_entry) == 0):
                # either we have a target entry and we found it comment it out
                # or we are in the target stanz and we comment the block...
                # python 2, do not add extra newlines...
                line = "#" + line
        print line,

def create_indexes_regex(index):
    if index.find(",") != -1:
        idx_regex = ""
        for idx in index.split(","):
            idx_regex = idx_regex + idx + "|"
        idx_regex = idx_regex[:-1]
        idx_regex = "(" + idx_regex + ")"
    else:
        idx_regex = index
    return idx_regex

# Provided with a file handle, stanza, index, dest_key and format write this into a transforms.conf file
# include a comment that this is auto-generated configuration...
def write_to_file(file, stanza, index, dest_key, format):
    logger.info("Writing to file=%s stanza=%s index=%s DEST_KEY=%s FORMAT=%s" % (file, stanza, index, dest_key, format))
    file.write("# Auto-generated configuration\n")
    file.write("[" + stanza + "]\n")
    file.write("SOURCE_KEY = _MetaData:Index\n")
    file.write("REGEX = ^" + create_indexes_regex(index) + "$\n")
    file.write("DEST_KEY = " + dest_key + "\n")
    file.write("FORMAT = " + format + "\n")

logger.info("Script begins with arguments: %s" % (args))

# load configuration
with open(args.configfile) as json_data_file:
    data = json.load(json_data_file)

# Sanity check config file
for entry in data:
    entry_config = data[entry]
    if not 'output_name' in entry_config or not 'config' in entry_config or not 'index_name' in entry_config:
        logger.error("entry=%s is invalid in configuration, config, index_name and output_name is all required for this to work" % (entry))
        sys.exit(1)
    if not entry_config['config'] == 'route' and not entry_config['config'] == 'nullQueue':
        logger.error("entry=%s is invalid in configuration, config option can only be route or nullQueue" % (entry))
        sys.exit(2)
    if 'output_type' in entry_config and entry_config['output_type'] == "TCP" and not 'default_output' in entry_config:
        logger.warn("entry=%s is using TCP routing but not a default_output, note this will override routing to only %s and therefore the default route will not get a copy unless add_mode is specified in the config *and* the entry already has a routing entry..." % (entry, entry_config['output_name']))
        logger.warn("To prevent this situation add the default_output option into the json config file")

props_files = []
transforms_files = []
inputs_files = []

# Walk the filesystem location and find the props.conf, transforms.conf and inputs.conf files
logger.debug("Walking directory %s" % (args.location))
for root, dirnames, filenames in os.walk(args.location):
    for filename in fnmatch.filter(filenames, 'props.conf'):
        file = os.path.join(root, filename)
        props_files.append(file)
        logger.debug("Found props file=" + file)
    for filename in fnmatch.filter(filenames, 'transforms.conf'):
        file = os.path.join(root, filename)
        transforms_files.append(file)
        logger.debug("Found transforms file=" + file)
    for filename in fnmatch.filter(filenames, 'inputs.conf'):
        file = os.path.join(root, filename)
        inputs_files.append(file)
        logger.debug("Found inputs file=" + file)

# parse all props.conf files
props_config = ConfigParser(allow_no_value=True, strict=False)
props_config.optionxform = str
props_config.read(props_files)
logger.debug("Sections of props_config file include: %s" % (props_config.sections()))

props_entries = {}

# Create a dictionary of props.conf stanzas that contain the TRANSFORMS- lines
# it is possible to have more than one TRANSFORMS- line within a stanza...
for sec in props_config.sections():
    options = props_config.options(sec)
    logger.debug("Options of section %s are %s" % (sec, options))
    for option in options:
        if option.find("TRANSFORMS-") == 0:
            value = props_config.get(sec, option)
            logger.debug("option=%s value=%s" % (option, value))
            if sec in props_entries:
                props_entries[sec].extend([ val.strip() for val in value.split(",") ])
            else:
                props_entries[sec] = [ val.strip() for val in value.split(",") ]

logger.debug("All props entries: %s" % (props_entries))

# transforms config is more straightforward, we just need to know what exists in the transforms.conf files
transforms_config = ConfigParser(allow_no_value=True, strict=False)
transforms_config.optionxform = str
transforms_config.read(transforms_files)
logger.debug("Sections of transforms_config file include: %s" % (transforms_config.sections()))

# inputs.conf files, do we have anything that we need to re-route? Or stop completely via commenting it out?
inputs_config = ConfigParser(allow_no_value=True, strict=False)
inputs_config.optionxform = str
inputs_config.read(inputs_files)
logger.debug("Sections of inputs_config file include: %s" % (inputs_config.sections()))

inputs_sourcetype_dict = {}
for stanza in inputs_config.sections():
    if inputs_config.has_option(stanza, 'index') and inputs_config.has_option(stanza, 'sourcetype'):
        index = inputs_config.get(stanza, 'index')
        sourcetype = inputs_config.get(stanza, 'sourcetype')
        logger.debug("stanza=%s index=%s sourcetype=%s" % (stanza, index, sourcetype))
        if not sourcetype in inputs_sourcetype_dict:
            inputs_sourcetype_dict[sourcetype] = []
        inputs_sourcetype_dict[sourcetype].append(stanza)

# keep a list of entries that we need to work on later
entries_not_found = []
input_entries_not_found = {}

for stanza in data:
    # the config file may have multiple entries for the same stanza with unique names
    if 'name' in data[stanza]:
        stanza_name = data[stanza]['name']
        logger.debug("Overriding from stanza=%s to name provided in config of stanza=%s" % (stanza, stanza_name))
    else:
        stanza_name = stanza

    if 'remove_mode' in data[stanza]:
        remove_mode = True
    else:
        remove_mode = False

    # if we see an inputs.conf entry that matches the sourcetype in question...
    if stanza_name in inputs_sourcetype_dict:
        stanza_list_in_config = inputs_sourcetype_dict[stanza_name]
        logger.debug("Found an inputs.conf entry related to this sourcetype/index combination=%s" % (stanza_list_in_config))

        # we now have a list of potential inputs.conf stanzas to work with
        for stanza_in_config in stanza_list_in_config:
            # for the stanza we found does it match the index we're looking at?
            index_in_config = inputs_config.get(stanza_in_config, 'index')
            index_name = data[stanza]['index_name']
            found_input = False
            if not index_name == index_in_config:
                logger.debug("index=%s on stanza=%s does not match config option of index=%s" % (index_name, stanza_in_config, index_in_config))
                continue

            output_name = data[stanza]['output_name']
            if inputs_config.has_option(stanza_in_config, '_TCP_ROUTING'):
                #We have a _TCP_ROUTING entry, does it match what we want?
                tcp_routing_settings = inputs_config.get(stanza_in_config, '_TCP_ROUTING')
                tcp_routing_settings_list = [ val.strip() for val in tcp_routing_settings.split(",") ]
                logger.debug("tcp_routing_settings_list=%s for inputs.conf stanza=%s" % (tcp_routing_settings_list, stanza_in_config))
                if output_name in tcp_routing_settings_list and 'output_type' in data[stanza] and data[stanza]['output_type'] == "TCP":
                    logger.info("Found stanza=%s index=%s inputs.conf stanza=%s _TCP_ROUTING=%s as expected" % (stanza, index_name, stanza_in_config, tcp_routing_settings_list))
                    found_input = True
            elif inputs_config.has_option(stanza_in_config, '_SYSLOG_ROUTING'):
                syslog_routing_settings = inputs_config.get(stanza_in_config, '_SYSLOG_ROUTING')
                syslog_routing_settings_list = [ val.strip() for val in syslog_routing_settings.split(",") ]
                logger.debug("syslog_routing_settings_list=%s for inputs.conf stanza=%s" % (syslog_routing_settings_list, stanza_in_config))
                if output_name in syslog_routing_settings_list and 'output_type' in data[stanza] and data[stanza]['output_type'] == "syslog":
                    logger.info("Found stanza=%s index=%s inputs.conf stanza=%s _SYSLOG_ROUTING=%s as expected" % (stanza, index_name, stanza_in_config, syslog_routing_settings_list))
                    found_input = True
            if not found_input and not remove_mode:
                logger.info("Unable to find an entry for stanza=%s index=%s in inputs.conf routing to output=%s adding inputs.conf stanza=%s to the list" % (stanza, index_name, output_name, stanza_in_config))
                input_entries_not_found[stanza_in_config] = stanza
            elif found_input and remove_mode:
                logger.info("Found entry for stanza=%s index=%s in inputs.conf routing to output=%s adding inputs.conf stanza=%s to the list to remove entry" % (stanza, index_name, output_name, stanza_in_config))
                input_entries_not_found[stanza_in_config] = stanza

    if stanza_name in props_entries:
        # we know that there is at least 1 TRANFORMS- entry within the required stanza
        transforms_name = props_entries[stanza_name]

        found = False
        for transform in transforms_name:
            if transform in transforms_config.sections():
                the_transform = transforms_config.options(transform)
                logger.debug("transform: %s" % (the_transform))
                if "DEST_KEY" in the_transform:
                    dest_key = transforms_config.get(transform, "DEST_KEY")
                    logger.debug("DEST_KEY=%s" % (dest_key))
                    if (dest_key == "_TCP_ROUTING" and 'output_type' in data[stanza] and data[stanza]['output_type'] == "TCP") \
                        or (dest_key == "_SYSLOG_ROUTING" and 'output_type' in data[stanza] and data[stanza]['output_type'] == "syslog") \
                        or (dest_key == "queue" and data[stanza]['config'] == "nullQueue"):
                       if "SOURCE_KEY" in the_transform:
                           source_key = transforms_config.get(transform, "SOURCE_KEY")
                           logger.debug("SOURCE_KEY=%s" % (source_key))
                           if source_key == "_MetaData:Index":
                               if "REGEX" in the_transform:
                                   regex = transforms_config.get(transform, "REGEX")
                                   logger.debug("REGEX=%s" % (regex))
                                   idx_regex = create_indexes_regex(data[stanza]['index_name'])
                                   expected_regex = "^(" + idx_regex + ")$"
                                   alt_regex = "^" + idx_regex + "$"
                                   if regex == expected_regex or regex==alt_regex:
                                       format = transforms_config.get(transform, "FORMAT")
                                       format_list = [ val.strip() for val in format.split(",") ]
                                       if data[stanza]['output_name'] in format_list:
                                           found = True
                                           logger.info("For stanza=%s found transform=%s REGEX=%s SOURCE_KEY=%s, DEST_KEY=%s FORMAT=%s" % (stanza_name, transform, regex, source_key, dest_key, format))
                                       else:
                                           logger.debug("For stanza=%s found transform=%s REGEX=%s SOURCE_KEY=%s, DEST_KEY=%s FORMAT=%s" % (stanza_name, transform, regex, source_key, dest_key, format))
        if found:
            logger.info("For stanza=%s found an entry performing the required routing" % (stanza_name))
            if remove_mode:
                logger.info("stanza=%s will be added to remove list" % (stanza_name))
                entries_not_found.append(stanza)
        else:
            logger.info("For stanza=%s, did not find an entry in props/transforms location=%s to perform the required routing for config stanza=%s" % (stanza_name, args.location, stanza))
            entries_not_found.append(stanza)
    else:
        logger.info("No props.conf stanza containing a TRANSFORMS- entry was found for stanza=%s in location=%s for config entry=%s" % (stanza_name, args.location, stanza))
        if not remove_mode:
            entries_not_found.append(stanza)

logger.info("The following props.conf stanzas did not have transforms.conf stanzas matching the required index_name: %s" % (entries_not_found))
logger.info("The following inputs.conf stanzas did not have the required _TCP_ROUTING or _SYSLOG_ROUTING that was requested: %s" % (input_entries_not_found))

# At this point we know what does/does not exist, was this check mode?
if args.mode=='check':
    logger.info("Checking completed")
    sys.exit(0)

# If we need to output a new file we can use the ConfigParser...
new_config_parser = ConfigParser(allow_no_value=True, strict=False)
# perform case sensitive matching
new_config_parser.optionxform = str
new_config_req = False

# keep dictionaries to track what must be output for props.conf and transforms.conf files
props_file_update_dict = {}
transform_file_update_dict = {}

#Do not attempt to re-add duplicate sections into the new config files
new_sections_added = []

# We are trying to make config updates to handle the routing requirements we have...
for stanza in entries_not_found:
    index_name = data[stanza]['index_name']
    output_name = data[stanza]['output_name']
    if 'output_type' in data[stanza]:
        output_type = data[stanza]['output_type'].lower()
    config = data[stanza]['config']

    if 'name' in data[stanza]:
        stanza_name = data[stanza]['name']
        logger.debug("Overriding from stanza=%s to name provided in config of stanza=%s" % (stanza, stanza_name))
    else:
        stanza_name = stanza

    if data[stanza]['config'] == 'route':
        config_entry = "route_" + output_name + "_" + output_type
        if index_name.find(",") == -1:
            config_entry = config_entry + "_" + index_name
        else:
            name = ""
            for idx in index_name.split(","):
                name = name + idx[:4] + "_" + idx[:-4] + "_"
            if len(name) > 200:
                name = name[:100] + name[-100:]
            config_entry = config_entry + "_" + name
        default_option = "TRANSFORMS-route"
    else:
        if index_name.find(",") == -1:
            config_entry = "null_" + index_name
        else:
            config_entry = "null_multipleindexes"
        default_option = "TRANSFORMS-null"

    transform_option = False

    if 'remove_mode' in data[stanza]:
        remove_mode = True
    else:
        remove_mode = False

    filename = False
    props_remove_update = False

    if props_config.has_section(stanza_name):
        # we do not know which file but we know the stanza exists somewhere...
        logger.debug("stanza=[%s] is defined within config" % (stanza_name))

        if stanza_name in props_entries:
            # We know there is one or more TRANSFORMS- entries at this point
            options = props_config.options(stanza_name)
            chosen_transform = False
            possible_options = []
            preferred_option = False
            idx_regex = create_indexes_regex(index_name)
            expected_regex = "^" + idx_regex + "$"
            alt_regex = "^(" + idx_regex + ")$"

            for option in options:
                # if we have a transform called route use that one
                if option.find("TRANSFORMS-") == 0:
                    possible_options.append(option)
                    # In add_mode we may want to add one more entry into the FORMAT= of the transform
                    # if we have a transform that is updating the TCP or syslog routing...
                    # this makes things more complicated as we cannot just add a new transforms.conf stanza + update the existing props.conf file
                    if ('add_mode' in data[stanza] and config == 'route') or (remove_mode and config == 'route'):
                        # obtain the list of TRANSFORMS-example = x,y,z
                        transforms_value = props_config.get(stanza_name, option)
                        logger.debug("props.conf stanza=%s for option=%s found transforms values=%s" % (stanza_name, option, transforms_value))
                        transform_list = [ val.strip() for val in transforms_value.split(",") ]
                        for a_transform in transform_list:
                            if transforms_config.has_option(a_transform, 'DEST_KEY'):
                                dest_key = transforms_config.get(a_transform, 'DEST_KEY')
                                logger.debug("props.conf stanza=%s option=%s transforms.conf DEST_KEY=%s output_type=%s" % (stanza_name, option, dest_key, output_type))
                                # if a transform has the required routing key, we use that one...
                                if (dest_key == '_TCP_ROUTING' and output_type == "tcp") or\
                                   (dest_key == '_SYSLOG_ROUTING' and output_type == "syslog"):
                                    if transforms_config.has_option(a_transform, 'SOURCE_KEY') and transforms_config.get(a_transform, 'SOURCE_KEY') == "_MetaData:Index" \
                                    and (transforms_config.get(a_transform, 'REGEX') == expected_regex \
                                    or transforms_config.get(a_transform, 'REGEX') == alt_regex):
                                        logger.info("transform=%s is routing to the expected type of data, and the regex matches the index in question..." % (a_transform))
                                        transform_option = a_transform
                                        break
                                    elif transforms_config.has_option(a_transform, 'SOURCE_KEY'):
                                        logger.debug("transforms.conf stanza=%s option=%s transforms.conf SOURCE_KEY=%s" % (a_transform, option, transforms_config.get(a_transform, 'SOURCE_KEY')))
                                        if transforms_config.has_option(a_transform, 'REGEX'):
                                            logger.debug("transforms.conf stanza=%s REGEX=%s did not match regex=%s or regex=%s" % (a_transform, transforms_config.get(a_transform, 'REGEX'), expected_regex, alt_regex))

                    if transform_option != False:
                        if remove_mode and len(transform_list) == 1:
                            # we are in remove_mode but the TRANSFORM- entry needs to be changed as well as it only had 1 entry
                            props_remove_update = True
                        break
                # preferred names are TRANSFORMS-route or TRANSFORMS-null if they exist
                if (option.find("route") and data[stanza]['config'] == 'route') or (option.find("null") and data[stanza]['config'] == 'nullQueue'):
                    preferred_option = option
            if not transform_option and not remove_mode:
                # If we didn't find one use the first transform as updating an existing TRANSFORM- is preferred over creating new ones
                if not preferred_option:
                    preferred_option = possible_options[0]
                logger.debug("preferred_option=%s will be updated with the new routing option" % (preferred_option))
                chosen_config = props_config.get(stanza_name, preferred_option)

                # The chosen config will have one or more options, we just want to add one more...if this is not already added by a previous iteration
                if chosen_config.find(config_entry) == -1:
                    new_config = chosen_config + ", " + config_entry
                else:
                    logger.info("stanza-name=%s, option=%s config_entry=%s was found in the chosen_config=%s" % (stanza_name, option, config_entry, chosen_config))
                    new_config = chosen_config
                # At this point we now that the required stanza is in a props.conf somewhere, but we cannot be sure which props.conf it is
                # therefore we need to locate the individual file and parse only *it* so we can then output it with any required updates
                filename = get_config_file(props_files, stanza_name, preferred_option)

                # Is this file already in the list of files that we need to update?
                if not filename in props_file_update_dict:
                    props_file_update_dict[filename] = {}
                # Is this stanza in the dict already?
                if not stanza_name in props_file_update_dict[filename]:
                    props_file_update_dict[filename][stanza_name] = {}
                # Is the option/entry from the stanza that we want to update already in the update list?
                if not preferred_option in props_file_update_dict[filename][stanza_name]:
                    # It is not, our config goes in as-is
                    props_file_update_dict[filename][stanza_name][preferred_option] = {}
                    props_file_update_dict[filename][stanza_name][preferred_option]['config'] = new_config
                else:
                    # There is config getting updated so we just need to add our new entry to the end of the current config
                    props_file_update_dict[filename][stanza_name][preferred_option]['config'] = props_file_update_dict[filename][stanza_name][preferred_option]['config'] + ", " + config_entry

                logger.info("Adding file=%s to update list new_config=%s added to entry=%s within stanza=%s" % (filename, props_file_update_dict[filename][stanza_name][preferred_option]['config'], preferred_option, stanza_name))
            elif transform_option != False:
                filename = get_config_file(props_files, stanza_name, option)
                if remove_mode:
                    keyword1 = "will"
                    keyword2 = "removed from"
                    # Is this file already in the list of files that we need to update?
                    if not filename in props_file_update_dict:
                        props_file_update_dict[filename] = {}
                    if not stanza_name in props_file_update_dict[filename]:
                        props_file_update_dict[filename][stanza_name] = {}
                    # Is the option/entry from the stanza that we want to update already in the update list?
                    if not preferred_option in props_file_update_dict[filename][stanza_name]:
                        # It is not, our config goes in as-is
                        props_file_update_dict[filename][stanza_name][option] = {}
                        props_file_update_dict[filename][stanza_name][option]['config'] = 'remove'
                else:
                    keyword1 = "will not"
                    keyword2 = "added to"
                logger.info("props.conf file %s need an update as transform stanza=%s will have the additional output %s in file=%s" % (keyword1, transform_option, keyword2, filename))
        elif not remove_mode:
            # else there is no current TRANSFORMS- entries for this entry with the said stanza...
            filename = get_config_file(props_files, stanza_name)
            # Is this file already in the list of files that we need to update?
            if not filename in props_file_update_dict:
                props_file_update_dict[filename] = {}
            # Is the stanza we want to update already in the update list?
            if not stanza_name in props_file_update_dict[filename]:
                props_file_update_dict[filename][stanza_name] = {}
            # Is the option/entry from the stanza that we want to update already in the update list?
            if not default_option in props_file_update_dict[filename][stanza_name]:
                # It is not, our config goes in as-is
                props_file_update_dict[filename][stanza_name][default_option] = {}
                props_file_update_dict[filename][stanza_name][default_option]['config'] = config_entry
                props_file_update_dict[filename][stanza_name][default_option]['default_option'] = True
            else:
                # There is config getting updated so we just need to add our new entry to the end of the current config
                #if props_file_update_dict[filename][stanza_name][default_option]['config'].find(config_entry) == -1:
                props_file_update_dict[filename][stanza_name][default_option]['config'] = props_file_update_dict[filename][stanza_name][default_option]['config'] + ", " + config_entry
                #else:
                #     logger.warn("Config entry filename=%s stanza_name=%s default_option=%s config_entry=%s" % (filename, stanza_name, default_option, config_entry))
            logger.info("Adding file=%s to update list new_config=%s added to entry=%s within stanza=%s" % (filename, props_file_update_dict[filename][stanza_name][default_option]['config'], default_option, stanza_name))
    elif not remove_mode:
        # else we never had a props.conf entry found so we have to create one...
        if not args.configdir:
            logger.error("The stanza=%s is not found in any props.conf file and the -configdir argument was not passed in. Please pass in -configdir or create the stanza within a props.conf file" \
            % (stanza_name))
            sys.exit(3)
        filename = args.configdir + '/props.conf'
        new_config_req = True
        logger.info("file=%s attempting to create new stanza=%s with option %s=%s" % (filename, stanza_name, default_option, config_entry))
        if not stanza_name in new_sections_added:
            new_config_parser.add_section(stanza_name)
            new_sections_added.append(stanza_name)
        if new_config_parser.has_option(stanza_name, default_option):
            new_config_options = new_config_parser.get(stanza_name, default_option)
            new_config_opt_list = [ val.strip() for val in new_config_options.split(",") ]
            if config_entry not in new_config_opt_list:
                logger.info("Appending option=%s into stanza=%s entry=%s" % (config_entry, stanza_name, default_option))
                new_config_entry = new_config_options + ", " + config_entry
                new_config_parser.set(stanza_name, default_option, new_config_entry)
            else:
                logger.debug("option=%s already in stanza=%s entry=%s" % (config_entry, stanza_name, default_option))
        else:
            new_config_parser.set(stanza_name, default_option, config_entry)

    if remove_mode and not filename:
        # we do not have a filename that contains our configuration that needs removing so we can stop at this point
        logger.debug("filename not specified and remove_mode is true, skipping this iteration of the loop")
        continue

    # At this point we want to add in the transforms.conf entry we need into the required file
    # we know that the transforms.conf stanza we want doesn't exist yet so we create it and hope that our naming standard is unique...
    filename = filename[0:filename.rfind("/")] + "/transforms.conf"

    # either we are nullQueue or we're routing somewhere
    if config == "nullQueue":
        dest_key = "queue"
    elif output_type == "tcp":
        dest_key = "_TCP_ROUTING"
    else:
        dest_key = "_SYSLOG_ROUTING"
        logger.info("Please note that output to a SYSLOG using TCP can block Splunk output queues, until https://ideas.splunk.com/ideas/EID-I-144 is implemented you may wish to use UDP+SYSLOG or TCP+dropEventsOnQueueFull settings")

    # if we have a default_output set combine it with our output_name from the config
    if 'default_output' in data[stanza]:
        output_name = data[stanza]['default_output'] + ", " + output_name

    if 'add_mode' in data[stanza]:
        add_mode = True
    else:
        add_mode = False

    # if the transform file is not listed already
    if not filename in transform_file_update_dict:
        transform_file_update_dict[filename] = {}

    if transform_option:
        # since we are no longer creating the new transforms.conf stanza (variable config_entry) override it with the existing transform we will use
        config_entry = transform_option
    # if we don't have an entry for this particular transform stanza name
    if not config_entry in transform_file_update_dict[filename]:
        transform_file_update_dict[filename][config_entry] = {}

    logger.debug("updating transform_file_update_dict filename=%s config_entry=%s index_name=%s dest_key=%s output_name=%s add_mode=%s remove_mode=%s" %
                (filename, config_entry, index_name, dest_key, output_name, add_mode, remove_mode))
    # We might be updating an existing entry but the transform names should be unique so this should be harmless
    transform_file_update_dict[filename][config_entry]['index_name'] = index_name
    transform_file_update_dict[filename][config_entry]['dest_key'] = dest_key
    transform_file_update_dict[filename][config_entry]['output_name'] = output_name
    transform_file_update_dict[filename][config_entry]['add_mode'] = add_mode
    transform_file_update_dict[filename][config_entry]['remove_mode'] = remove_mode
    transform_file_update_dict[filename][config_entry]['transform_name'] = transform_option

# for each props.conf file we need to work with
for filename in props_file_update_dict:
    for stanza in props_file_update_dict[filename]:
        # find the relevant TRANSFORM- line to be updated
        for entry in props_file_update_dict[filename][stanza]:
            updated_line = props_file_update_dict[filename][stanza][entry]['config']
            if 'default_option' in props_file_update_dict[filename][stanza][entry]:
                new_entry = True
            else:
                new_entry = False

            if args.mode == 'simulate':
                if updated_line == 'remove':
                    keyword = "remove stanza from"
                else:
                    keyword = "update"
                logger.info("would %s filename=%s stana=%s entry=%s new_line=%s new_entry=%s" % (keyword, filename, stanza, entry, updated_line, new_entry))
            else:
                if updated_line == 'remove':
                    # the TRANSFORMS- in question needs to be commented out as we are removing it from the config...
                    comment_config_file(filename, stanza, entry)
                else:
                    logger.debug("updating file filename=%s stanza=%s entry=%s" % (filename, stanza, entry))
                    # actually update the file
                    update_config_file(filename, stanza, entry, updated_line, new_entry)

# If we have new props.conf configuration that doesn't fit into any existing file write it all out now, it goes into a common file...
if new_config_req:
    filename = args.configdir + '/props.conf'

    if args.mode == 'simulate':
        for sec in new_config_parser.sections():
            for option in new_config_parser.options(sec):
                logger.info("would update filename=%s stanza=%s entry=%s value=%s" % (filename, sec, option, new_config_parser.get(sec, option)))
    else:
        logger.info("Writing to configfile=%s" % (filename))
        with open(filename, 'a+') as configfile:
            new_config_parser.write(configfile)

transform_stanzas = []

for filename in transform_file_update_dict:
    for stanza in transform_file_update_dict[filename]:
        # the transform stanzas can appear under multiple props.conf (and therefore transforms.conf file)
        # however we only need 1 of them written to the config
        if stanza in transform_stanzas:
            logger.info("stanza=%s has being written to a file, not writing it in file=%s" % (stanza, filename))
            continue
        transform_stanzas.append(stanza)
        shortcut = transform_file_update_dict[filename][stanza]
        index_name = shortcut['index_name']
        dest_key = shortcut['dest_key']
        output_name = shortcut['output_name']
        add_mode = shortcut['add_mode']
        remove_mode = shortcut['remove_mode']
        transform_name = shortcut['transform_name']

        logger.debug("working with filename=%s transform stanza=%s index_name=%s dest_key=%s output_name=%s add_mode=%s" % (filename, stanza, index_name, dest_key, output_name, add_mode))
        # We want to only add new entries to this and not remove any existing outputs...
        if add_mode and transforms_config.has_option(stanza, "FORMAT"):
            format = transforms_config.get(stanza, "FORMAT")
            format_list = [ val.strip() for val in format.split(",") ]
            output_list = [ val.strip() for val in output_name.split(",") ]
            new_output_list = list(set(format_list+output_list))
            output_name = ", ".join(new_output_list)
        elif remove_mode and transforms_config.has_option(stanza, "FORMAT"):
            format = transforms_config.get(stanza, "FORMAT")
            format_list = [ val.strip() for val in format.split(",") ]
            output_list = [ val.strip() for val in output_name.split(",") ]
            new_output_list = list(set(format_list) - set(output_list))
            output_name = ", ".join(new_output_list)

        if args.mode == 'simulate':
            if add_mode:
                logger.info("would update filename=%s stanza=%s FORMAT=%s" % (filename, stanza, output_name))
            elif output_name == "":
                logger.info("would comment out stanza=%s in filename=%s" % (stanza, filename))
            else:
                logger.info("would update filename=%s stanza=%s REGEX=^%s$ DEST_KEY=%s FORMAT=%s" % (filename, stanza, index_name, dest_key, output_name))
        else:
            if add_mode and transform_name != False:
                update_config_file(filename, stanza, "FORMAT", output_name)
            elif remove_mode:
                comment_config_file(filename, stanza)
            else:
                # if the file does exist we append an include a newline...
                if os.path.isfile(filename):
                    with open(filename, 'a') as file:
                        file.write("\n")
                        write_to_file(file, stanza, index_name, dest_key, output_name)
                else:
                    with open(filename, 'w') as file:
                        write_to_file(file, stanza, index_name, dest_key, output_name)

# we have dealt with the props/transforms side, but what about inputs.conf files? Sometimes we can route directly from inputs.conf
for input_stanza in input_entries_not_found:
    filename = get_config_file(inputs_files, input_stanza)
    logger.debug("filename=%s needs an update for inputs.conf stanza=%s" % (filename, input_stanza))

    data_entry = input_entries_not_found[input_stanza]
    if 'name' in data[data_entry]:
        name = data[data_entry]['name']
    else:
        name = data_entry
    output_name = data[data_entry]['output_name']
    config = data[data_entry]['config']

    if config == "nullQueue":
        output_type = "nullQueue"
    else:
        output_type = data[data_entry]['output_type'].lower()

    new_entry = True
    if config == "nullQueue":
        # if we attempt to nullQueue an inputs.conf entry we actually want to comment out the said entry
        logger.info("filename=%s stanza=%s requires commenting out / removal" % (filename, input_stanza))
    elif inputs_config.has_option(input_stanza, '_TCP_ROUTING') and output_type == 'tcp':
        new_entry = False
        output_entry = inputs_config.get(input_stanza, '_TCP_ROUTING')

        if 'default_output' in data[data_entry]:
            output_name = output_name + ", " + data[data_entry]['default_output']
        else:
            logger.warn("entry=%s is using TCP routing but not a default_output, note this will override routing to only %s and therefore the default route will not get a copy unless add_mode is specified in the config *and* the entry already has a routing entry..." % (data_entry, output_name))
            logger.warn("To prevent this situation add the default_output option into the json config file")

    elif inputs_config.has_option(input_stanza, '_SYSLOG_ROUTING') and output_type == 'syslog':
        new_entry = False
        output_entry = inputs_config.get(input_stanza, '_SYSLOG_ROUTING')
        if 'default_output' in data[data_entry]:
            output_name = output_name + ", " + data[data_entry]['default_output']

    if config != "nullQueue" and not new_entry and 'add_mode' in data[data_entry]:
        output_entry_list = [ val.strip() for val in output_entry.split(",") ]
        output_list = [ val.strip() for val in output_name.split(",") ]
        new_output_list = list(set(output_entry_list+output_list))
        output_name = ", ".join(new_output_list)
    elif config != "nullQueue" and not new_entry and 'remove_mode' in data[data_entry]:
        output_entry_list = [ val.strip() for val in output_entry.split(",") ]
        output_list = [ val.strip() for val in output_name.split(",") ]
        new_output_list = list(set(output_entry_list) - set(output_list))
        output_name = ", ".join(new_output_list)

    if output_type == 'tcp':
        key = "_TCP_ROUTING"
    elif output_type == 'syslog':
        key = "_SYSLOG_ROUTING"
        if not new_entry:
            logger.info("filename=%s stanza=%s _SYSLOG_ROUTING only works on Splunk enterprise servers and not Universal Forwarders, please ensure this inputs.conf is not on a UF" % (filename, input_stanza))
    else:
        logger.info("output_type=%s" % (output_type))

    if args.mode == 'simulate':
        if config == "nullQueue":
            logger.info("would comment out stanza=%s in filename=%s" % (input_stanza, filename))
        else:
            if 'remove_mode' in data[data_entry] and output_name == "":
                logger.info("would update filename=%s stanza=%s and comment out entry=%s" % (filename, input_stanza, key))
            else:
                logger.info("would update filename=%s stanza=%s entry=%s value=%s new_entry=%s" % (filename, input_stanza, key, output_name, new_entry))
    else:
        if config == "nullQueue":
            comment_config_file(filename, input_stanza)
            logger.info("Updated filename=%s stanza=%s, it is now commented out" % (filename, input_stanza))
        else:
            if 'remove_mode' in data[data_entry] and output_name == "":
                comment_config-file(filename, input_stanza, key)
            else:
                update_config_file(filename, input_stanza, key, output_name, new_entry)
            logger.info("Updated filename=%s stanza=%s entry=%s=%s new=%s" % (filename, input_stanza, key, output_name, new_entry))

logger.info("Script complete")
