#!/usr/bin/env python3
# coding: utf-8

# Simple python script using Splunk lookup-editor https://splunkbase.splunk.com/app/1724 rest endpoint to upload lookups (wiki https://lukemurphey.net/projects/splunk-lookup-editor/wiki/REST_endpoints)
# troubleshoot lookup-editor errors on splunk: index=_internal (sourcetype=lookup_editor_rest_handler OR sourcetype=lookup_backups_rest_handler) 
# Based on https://github.com/mthcht/lookup-editor_scripts, customised by Gareth Anderson
# Note this is designed to sync lookup files, but not the permissions beyond sharing/owner (as in read/write permissions will be left as defaults)
# empty files will fail to upload (they can download successfully using the download script)
# For KV Store collections refer to KV Store Tools Redux / https://splunkbase.splunk.com/app/5328

import json
import requests
import csv
import re
import logging
import pathlib
import os
import argparse
import time
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# Disable warning for insecure requests
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Setup logging
logging.basicConfig(level=logging.INFO)

# Methods
parser = argparse.ArgumentParser(description="Script download_lookups_from_splunk.py: download multiple lookups from Splunk, can be used for automation processes")
parser.add_argument("-app", "--app", help="Specify the Splunk application, default to 'search' app", required=True)
parser.add_argument("-f", "--folder", help="Folder path to upload the lookup files from, default to same directory", default=".")
parser.add_argument("-t", "--token", help="Token to use on Splunk instance to download the lookup files", required=True)
parser.add_argument("-u", "--url", help="Endpoint to download the lookups from (https://localhost:8089)", required=True)

args = parser.parse_args()


# GET requests to this endpoint will execute get_lookup_contents() and POST requests to this endpoint will execute post_lookup_contents() from the lookup_editor_rest_handler.py in the lookup-editor app
splunk_management_service = "/services/data/lookup_edit/lookup_contents" #endpoint lookup-editor 

splunk_app = args.app 
lookup_folder = args.folder 

for lookup_sharing in pathlib.Path(lookup_folder).iterdir():
    for lookup_owner in lookup_sharing.iterdir():
        for lookup_file in lookup_owner.iterdir():
            lookup_name = pathlib.Path(lookup_file).name
            lookup_name = str(lookup_name.replace(" ","_"))

            # Read data from CSV file
            lookup_content = []
            try:
                with open(lookup_file, encoding='utf-8', errors='ignore',newline='') as f:
                    reader = csv.reader(f, delimiter=',')
                    for row in reader:
                        lookup_content.append(row)
            except  Exception as e:
                logging.error("Error reading {} : {}".format(lookup_file,e))

            # Send POST request to Splunk server, we use nobody to use app level sharing
            # as I cannot find a way to advise of the sharing level...
            try:
                if lookup_sharing.name == "user":
                    owner = lookup_owner.name
                else:
                    owner = "nobody"
                # I cannot find any part of this endpoint that sets the "sharing" setting
                if len(lookup_content) == 0:
                    logging.info(f"Lookup file {lookup_file} is zero sized, re-creating zero sized lookup using outputlookup in app {splunk_app} with owner {owner}")
                    r = requests.post(f"{args.url}/servicesNS/{owner}/{splunk_app}/search/jobs/export", data={ "search": f"| outputlookup {lookup_name}" },
                    verify=False,
                    headers={ "Authorization": f"Splunk {args.token}" },
                    timeout=60
                )
                else:
                    r = requests.post(f"{args.url}{splunk_management_service}", data={ "lookup_type": "csv", "namespace": splunk_app, "lookup_file": lookup_name, "owner": owner, "header_only": "false", "contents": json.dumps(lookup_content) },
                        verify=False,
                        headers={ "Authorization": f"Splunk {args.token}" },
                        timeout=60
                    )
                if r.status_code == 200:
                    logging.info("[success] file: \'{}\' uploaded to Lookup editor handler {} and saved in splunk app \'{}\'".format(lookup_name,r.url,splunk_app))
                else:
                    logging.error("[failed] file: \'{}\', status:{}, reason:{}, url:{}".format(lookup_name,r.status_code,r.reason,r.url))
            except Exception as e:
                logging.error("Error sending request to Splunk server: {}".format(e))

            # the lookup editor will upload the owner to a private lookup without an issue, skip the re-own process
            if lookup_sharing.name == "user":
                continue

            # you may be able to remove this line, I didn't have sticky sessions on the LB during testing
            logging.info("Sleeping for 8 seconds to allow any SHC replication")
            time.sleep(8)

            # since we may have the wrong sharing set, or the nobody-owned lookups may be owned by the person uploading we hit the ACL endpoint to fix it
            try:                
                r = requests.post(f"{args.url}/servicesNS/nobody/{splunk_app}/data/lookup-table-files/{lookup_name}/acl", data={ "owner": lookup_owner.name, "sharing": lookup_sharing.name },
                    verify=False,
                    headers={ "Authorization": f"Splunk {args.token}" },
                    timeout=60
                )
                if r.status_code == 200:
                    logging.info("[success] file: \'{}\' had ACL settings changed using url {} in splunk app \'{}\' to sharing {} with owner {}".format(lookup_name,r.url,splunk_app,lookup_sharing.name,lookup_owner.name))
                else:
                    logging.error("[failed] file: \'{}\', status:{}, reason:{}, url:{}".format(lookup_name,r.status_code,r.reason,r.url))
            except Exception as e:
                logging.error("Error sending request to Splunk server: {}".format(e))
