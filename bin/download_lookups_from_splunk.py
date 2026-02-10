#!/usr/bin/env python3
# coding: utf-8
# Simple python script using Splunk lookup-editor https://splunkbase.splunk.com/app/1724 rest endpoint to download lookups, can be used by automation processes (wokring on splunk cloud)
# Based on https://github.com/mthcht/lookup-editor_scripts, customised by Gareth Anderson

import requests
import csv
import logging
from requests.packages.urllib3.exceptions import InsecureRequestWarning
import time 
import argparse
import sys
import pathlib
import os

# Disable warning for insecure requests
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Setup logging
logging.basicConfig(level=logging.INFO)

# Methods
parser = argparse.ArgumentParser(description="Script download_lookups_from_splunk.py: download multiple lookups from Splunk, can be used for automation processes")
parser.add_argument("-app", "--app", help="Specify the Splunk application", required=True)
parser.add_argument("-f", "--folder", help="Folder path to save the lookups, default to same directory")
parser.add_argument("-l", "--lookups", nargs='+', help="Enter the name of the lookup(s) to download")
parser.add_argument("-t", "--token", help="Token to use on Splunk instance to download the lookup files", required=True)
parser.add_argument("-u", "--url", help="Endpoint to download the lookups from (https://localhost:8089)", required=True)

args = parser.parse_args()

# Check the arguments
if len(sys.argv)==1:
    # display help message when no args are passed.
    parser.print_help()
    print("example usages:\n./download_lookups_from_splunk.py\n./download_lookups_from_splunk.py -app search -t eySplunkTokenGoesHere -l Subnet_Scan_Exclusion_List.csv Port_Scan_Exclusion_List.csv\n\
./download_lookups_from_splunk.py -app search -f /home/mthcht/lookups/ -t eySplunkTokenGoesHere\n")
    logging.warning("warning: no arguments given to the script, will use default values declared in the script:")
 
# Declare variables (change them) 
splunk_app = args.app #splunk app name, example: search
splunk_management_service = "/services/data/lookup_edit/lookup_data"

if args.folder:
    lookup_folder = args.folder #folder path, it will contain all the lookups you want to download example (do not forget to put the / or \ at the end of the path and escape the \ for windows path) : "C:\\lookups\\" or "/home/mthcht/lookups/"
else:
    lookup_folder = pathlib.Path().absolute() #If no folder path, use default current directory of the script
lookup_type = "csv"
date = time.time()

if args.lookups:
    lookup_file_names_list = args.lookups
    logging.info("list of lookups to download: {}".format(lookup_file_names_list))
else:
    url=f"{args.url}/servicesNS/-/{splunk_app}/data/lookup-table-files?output_mode=json&search=eai:acl.app={splunk_app}&f=title"
    r = requests.get(url, verify=False, headers={ "Authorization": "Splunk " + args.token } ).json()
    lookup_file_names_list = []

    # lookup names are unique within an application at a sharing level/owner (i.e. multiple users can have the same lookup name and/or there may be the same lookup name at
    # user level and app level)
    for entry in r['entry']:
        lookup_file_names_list.append(f"{entry['name']}%%%{entry['acl']['owner']}%%%{entry['acl']['sharing']}")

# download lookups from Splunk
for lookup_file_name in lookup_file_names_list:
    try:
        lookup_file_data = lookup_file_name.split("%%%")
        lookup_name = lookup_file_data[0]
        if len(lookup_file_data) == 1:
            download_owner = "nobody"
            sharing = "app"
            owner = "nobody"
        elif lookup_file_data[2] == "user":
            download_owner = lookup_file_data[1]
            sharing = lookup_file_data[2]
            owner = lookup_file_data[1]
        else:
            download_owner = "nobody"
            sharing = lookup_file_data[2]
            owner = lookup_file_data[1]
       
        r = requests.post(f"{args.url}{splunk_management_service}", data={ "lookup_type": lookup_type, "namespace": splunk_app, "lookup_file": lookup_name, "header_only": "false", "owner": download_owner},
            verify=False,
            headers={ "Authorization": "Splunk " + args.token }
        )
    except Exception as e:
        logging.error("Error sending request to Splunk server: {}".format(e))

    if r.status_code == 200:
        logging.info("[success] file: \'{}\' in {} app has been downloaded from Lookup editor handler {} and will be saved in {}".format(lookup_name,splunk_app,r.url,lookup_folder))
        lookup_content_json_backup = r.json()
        # save the file downloaded
        try:
            lookup_file_name_downloaded = "{}.csv".format(lookup_name.split('.csv')[0])
            target_dir = os.path.join(lookup_folder, sharing, owner)
            # Create directory if it doesn't exist
            os.makedirs(target_dir, exist_ok=True)

            with open(f"{lookup_folder}/{sharing}/{owner}/{lookup_file_name_downloaded}", 'w', newline='') as myfile:
                wr = csv.writer(myfile)
                for row in lookup_content_json_backup:
                    wr.writerow(row)
                myfile.close
                logging.info("[sucess] File {} saved in folder {}".format(lookup_name,target_dir))
        except Exception as e:
            logging.error("[failed] Error: Cannot save a a backup of the file {} in {}, reason: {}".format(lookup_name,lookup_folder,e))
    else:
        logging.error("[failed] Error: Downloading file: \'{}\', status:{}, reason:{}, url:{}".format(lookup_name,r.status_code,r.reason,r.url))
