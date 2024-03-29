import logging
from logging.config import dictConfig
import argparse
import os
import re

####################################################################################################
#
# syslog_migration_to_hec_assistant
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
              'filename' : '/tmp/syslog_migration_to_hec_assistant.log',
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
parser.add_argument('-inputfile', help='Input file to read (inputs.conf)', required=True)
parser.add_argument('-debugMode', help='(optional) turn on DEBUG level logging (defaults to INFO)', action='store_true')
parser.add_argument('-syslogngdir', help='Directory to syslog-ng config to search', required=True)
parser.add_argument('-overwritemode', help='Do you want to overrwite the syslog-ng config files with updated HEC versions?', action='store_true')

args = parser.parse_args()

#If we want debugMode, keep the debug logging, otherwise drop back to INFO level
if not args.debugMode:
    logging.getLogger().setLevel(logging.INFO)

logger.info("syslog_migration_to_hec_assistant starts")

def parse_syslog_conf(conf_file):
    # if the file does not exist in this app we just return an empty list
    if not os.path.isfile(conf_file):
        logger.info(f"conf_file={conf_file} not found")
        return ""

    logger.info(f"working on conf_file={conf_file}")
    with open(conf_file, "r") as fp:
        file_match = ""
        text_match = ""
        # read through each line of the file
        for line in fp:
            logger.debug(f"Working with line={line} file={conf_file}")
            if line[0] == "#":
                continue

            # it's a stanza line [monitor:///]
            if line.find("file(\"") != -1 and line.find("key-file") == -1 and line.find("cert-file") == -1:
                # we're within the file line
                # rex match the parts we need if they exist such as template, file, 
                # map out the templates to the new hec templates where appropriate
                # print the updated HEC template
                # print file location
                file_match = re.match(r".*?file\(\"\s*([^\"]+)", line)
                template_match = re.match(r".*?template\(\s*([^\)]+)", line)
                logger.debug(f"file_match={file_match.group(1)}")

                if template_match:
                    text_match = template_match.group(1)
                    text_match = text_match + "_hec"
                    #print(text_match)

    if file_match == "":
        return "", text_match
    return file_match.group(1), text_match


files = os.listdir(args.syslogngdir)
files = [args.syslogngdir+'/'+f for f in files if os.path.isfile(args.syslogngdir+'/'+f)] #Filtering only the files.
logger.debug(f"syslog-files={files}")

all_syslog_files= {}
for a_file in files:
    all_syslog_files[a_file] = parse_syslog_conf(a_file)

#print(all_syslog_files)

# parse a single config file and extract any filtered lines
def parse_single_conf_file(conf_file, all_syslog_config):
    all_syslog_dest_config = {}

    # if the file does not exist in this app we just return an empty list
    if not os.path.isfile(conf_file):
        logger.info(f"conf_file={conf_file} not found")
        return {}

    with open(conf_file, "r") as fp:
        stanza_name = ""
        # read through each line of the file
        current_stanza = ""
        for line in fp:
            logger.debug(f"Working with line={line} file={conf_file}")
            if line[0] == "#":
                continue

            # it's a stanza line [monitor:///]
            if len(line) > 0 and line[0] == "[":
                if current_stanza != "":
                    for key in all_syslog_config.keys():
                        syslog_file_name, template = all_syslog_config[key]
                        # we sometimes use /.../ at the end of the line
                        if file_name.find("/.../") != -1:
                            logger.debug(f"before file_name={file_name}")
                            file_name = file_name[0:-5]
                            logger.debug(f"after file_name={file_name}")                        
                        if syslog_file_name.find(file_name) != -1:
                            #print(f"potential match with stanza_name={stanza_name} key={key} syslog_file_name={syslog_file_name} template={template}")
                            if host_segment != "":
                                syslog_file_name_split = syslog_file_name.split('/')
                                host_variable = syslog_file_name_split[int(host_segment)][1:]
                                logger.debug(f"host_variable={host_variable}")
                            else:
                                host_variable = "HOST"
                            if tcp_routing != "":
                                logger.debug(f"tcp_routing={tcp_routing}")
                            if index == "" or sourcetype == "":
                                logger.debug("index or sourcetype not set?")
                            if host_variable != "HOST":
                                template = template + "_" + host_variable                                
                            destination_name = syslog_file_name[16:syslog_file_name.find("/",17)]
                            source_name = syslog_file_name[0:syslog_file_name.find("/",17)]
                            new_dest = f"""destination d_hec_{destination_name} {{
    http(
        url("https://localhost:8088/services/collector/event")
        method("POST")
        log-fifo-size(100000000)
        workers(5)
        batch-lines(5000)
        batch-bytes(4096kb)
        batch-timeout(3000)
        timeout(30)
        user_agent("syslog-ng User Agent")
        headers("Authorization: Splunk d_hec_{destination_name}")
        persist-name("{destination_name}")
        disk-buffer(
            reliable(no)
            disk-buf-size(73400320)
            dir("/var/log/syslog/buffers/{destination_name}")
        )
        retries(5)
        tls(
            peer-verify(no)
        )
        #body('{{ "event": "$(template {template})", "source": "{source_name}", "time": "${{R_UNIXTIME}}", "host": "${{{host_variable}}}" }}')
        body('$(template {template})')        
    );
}};
""" 
                            all_syslog_dest_config[key] = [ new_dest, "d_hec_" + destination_name ]
                            file_short_name = key[key.rfind('/')+1:key.rfind(".")]
                            print(f"""
[http://syslog-{file_short_name}]
description=HEC token for {file_short_name}
token = d_hec_{destination_name} 
index = {index}indexes = {index}source = {source_name}\nsourcetype = {sourcetype.strip()}""")

                            if tcp_routing != "":
                                print(f"outputgroup={tcp_routing}")
                stanza_name = line[1:len(line)-2]                
                current_stanza = stanza_name
                file_name = stanza_name[10:]
                logger.debug(f"file_name={file_name}")
                logger.debug(f"working with stanza={stanza_name}")
                index = ""
                sourcetype = ""
                host_segment = ""
                tcp_routing = ""                            
            elif line.find("index") != -1:
                index = re.sub("\s*index\s*=\s*","",line)
                logger.debug(f"index={index}")
            elif line.find("sourcetype") != -1:
                sourcetype = re.sub("\s*sourcetype\s*=\s*","",line)
                logger.debug(f"sourcetype={sourcetype}")
            elif line.find("host_segment") != -1:
                host_segment = re.sub("\s*host_segment\s*=\s*","",line)
                logger.debug(f"host_segment={host_segment}")
            elif line.find("_TCP_ROUTING") != -1:
                tcp_routing = re.sub("\s*_TCP_ROUTING\s*=\s*","",line)
                logger.debug(f"tcp_routing={tcp_routing}")

    return all_syslog_dest_config

all_syslog_dest_config = parse_single_conf_file(args.inputfile, all_syslog_files)
#print(all_syslog_config)
#print(all_syslog_dest_config)

def replace_within_syslog_conf(conf_file, syslog_dest_config, dest_name):
    # if the file does not exist in this app we just return an empty list
    if not os.path.isfile(conf_file):
        logger.info(f"conf_file={conf_file} not found")
        return ""

    all_lines = ""
    logger.info(f"working on conf_file={conf_file}")
    with open(conf_file, "r") as fp:        
        # read through each line of the file
        inside_dest = False        
        for line in fp:
            logger.debug(f"Working with line={line} file={conf_file}")
            if line[0] == "#":
                all_lines = all_lines + line
                continue
            # it's a stanza line [monitor:///]
            if line.find("destination ") != -1 and line.find("{") != -1:
                inside_dest = True                            
            elif inside_dest and line.find("};") != -1:
                inside_dest = False
                all_lines = all_lines + '\n' + syslog_dest_config + '\n'
            elif inside_dest:
                continue
            elif line.find("destination(") != -1 or line.find("destination (") != -1:
                # this is a destination line so destination (d_file_udp_destname);
                # modify it to our new destination
                all_lines = all_lines + f"        destination({dest_name});\n"
            else:
                all_lines = all_lines + line
    return all_lines

for a_file in files:
    if a_file in all_syslog_dest_config:
        new_file = replace_within_syslog_conf(a_file, all_syslog_dest_config[a_file][0], all_syslog_dest_config[a_file][1])
        if args.overwritemode:
            with open(a_file, 'w') as f:
                f.write(new_file)
        else:
            print(new_file)
