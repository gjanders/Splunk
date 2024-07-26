#!/bin/sh
log=/opt/splunk/var/log/splunk/splunk_offline_script.log
echo "`date` splunk_offline script begins" | tee -a ${log}
#/opt/splunk/scripts/collect_stacks.sh -s 100 -b -o /opt/splunk/stacks &
# this requires a role with edit_indexer_cluster=enabled
/opt/splunk/bin/splunk offline -auth splunk_offline_automated:add_password_here 2>&1 | tee -a ${log}
#tail -n 10 /opt/splunk/var/log/splunk/splunkd.log | tee -a ${log}
/opt/splunk/bin/splunk status 2>&1 | tee -a ${log}
echo "`date` splunk_offline script exiting" | tee -a ${log}
# we run as root so change ownership back to splunk
chown splunk:splunk /opt/splunk/var/log/splunk/splunk_offline_script.log
